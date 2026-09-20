#!/usr/bin/env bash
# Temporary investigation helper: passt works on lab and fails on the AWS qemu
# hosts. Runs passt against a throwaway unix socket with a minimal client so
# both hosts are compared under identical conditions, and optionally traces the
# syscalls around accept() where the AWS run goes quiet.
# HOMELAB_PASST_PROBE_STRACE=1 installs and uses strace (disposable hosts only).
set -euo pipefail

sock=/tmp/passt_probe.sock
log=/tmp/passt_probe.log
trace=/tmp/passt_probe.strace

client_py=$(
  cat <<'PY'
import os, socket, struct, sys

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sys.argv[1])
print("client: connected, our pid is %d" % os.getpid())
# Broadcast ARP request for an address passt owns; a healthy passt answers it.
frame = (
    b"\xff" * 6
    + b"\x52\x54\x00\x12\x34\x56"
    + b"\x08\x06"
    + b"\x00\x01\x08\x00\x06\x04\x00\x01"
    + b"\x52\x54\x00\x12\x34\x56"
    + bytes(int(o) for o in sys.argv[2].split("."))
    + b"\x00" * 6
    + bytes(int(o) for o in sys.argv[3].split("."))
)
s.sendall(struct.pack(">I", len(frame)) + frame)
s.settimeout(5)
try:
    print("client: got %d bytes back" % len(s.recv(65536)))
except Exception as exc:  # noqa: BLE001 - probe reports whatever went wrong
    print("client: no reply (%s)" % exc)
PY
)

echo "### host"
uname -a
passt --version 2>&1 | head -2 || true
dpkg-query -W passt qemu-system-x86 2>&1 || true
sysctl kernel.apparmor_restrict_unprivileged_userns net.core.rmem_max net.core.wmem_max 2>&1 || true
sudo aa-status 2>/dev/null | grep -iE "passt|apparmor module" || echo "(no passt profile loaded)"

rm -f "$sock" "$log" "$trace"

echo "### limits"
ulimit -n
cat /proc/sys/fs/file-nr

echo "### passt profile"
sudo cat /etc/apparmor.d/usr.bin.passt 2>/dev/null || echo "(no profile file)"

strace_prefix=()
if [ "${HOMELAB_PASST_PROBE_STRACE:-}" = "1" ]; then
  sudo DEBIAN_FRONTEND=noninteractive timeout 180 apt-get install -y -qq strace >/dev/null 2>&1 || true
  if command -v strace >/dev/null; then
    strace_prefix=(strace -f -tt -o "$trace" -e "trace=network,desc")
  fi
fi

run_probe() {
  local label="$1"
  rm -f "$sock" "$log" "$trace"

  # Self-terminating so a wedged passt cannot hold the job to its timeout.
  timeout 15 "${strace_prefix[@]}" passt --socket "$sock" --foreground --trace >"$log" 2>&1 &
  local passt_pid=$!
  for _ in $(seq 40); do
    [ -S "$sock" ] && break
    sleep 0.25
  done

  # passt derives the guest and gateway addresses from the host route; reuse
  # them so the ARP request targets an address it actually answers for.
  local guest gw
  guest=$(awk '/assign:/ {print $2; exit}' "$log")
  gw=$(awk '/router:/ {print $2; exit}' "$log")
  echo "### ${label}: probing with guest=${guest} gateway=${gw}"
  python3 -c "$client_py" "$sock" "${guest:-0.0.0.0}" "${gw:-0.0.0.0}" || true
  wait "$passt_pid" 2>/dev/null || true

  echo "### ${label}: accepted connection lines"
  grep -c "accepted connection" "$log" || true
  echo "### ${label}: passt log"
  tail -15 "$log"
  if [ -s "$trace" ]; then
    echo "### ${label}: accept call"
    grep -E "accept4" "$trace" | head -5 || true
  fi
}

# Load the shipped profile first, so the probe starts from the same state
# whatever an earlier run on this reused instance left behind.
sudo apparmor_parser -r /etc/apparmor.d/usr.bin.passt 2>&1 || true

run_probe as_shipped

# Candidate fix: grant passt the AF_UNIX access its qemu socket needs. The
# profile predates this kernel mediating AF_UNIX, so accept4() is denied while
# the profile is still what grants passt its userns exception.
echo "### adding the missing unix rule"
sudo python3 - <<'FIX'
import pathlib

profile = pathlib.Path("/etc/apparmor.d/usr.bin.passt")
text = profile.read_text()
rule = "  unix (accept, bind, connect, listen, receive, send),\n"
if rule not in text:
    text = text[: text.rindex("}")] + rule + "}\n"
    profile.write_text(text)
print(text)
FIX
sudo apparmor_parser -r /etc/apparmor.d/usr.bin.passt && echo "reloaded"

run_probe fixed
