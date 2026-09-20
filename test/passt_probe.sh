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
  sed -n '/epoll event on listening/,$p' "$log" | head -6
  grep -E "unshare|Failed to sandbox|error" "$log" | head -3 || true
  if [ -s "$trace" ]; then
    echo "### ${label}: accept call"
    grep -E "accept4" "$trace" | head -5 || true
  fi
}

# Kernel audit records are rate-limited on these hosts, which is why the
# denial behind the EACCES never reaches the log.
sudo sysctl -w kernel.printk_ratelimit=0 kernel.printk_ratelimit_burst=0 >/dev/null || true

# Write the profile ourselves rather than patching the shipped file, so a
# reused instance an earlier probe already touched cannot skew the result.
write_profile() {
  sudo tee /etc/apparmor.d/usr.bin.passt >/dev/null <<EOF
abi <abi/3.0>,
include <tunables/global>
profile passt /usr/bin/passt{,.avx2} {
  include <abstractions/passt>
  owner /tmp/** w,
  owner @{HOME}/** w,
${1}
}
EOF
  sudo apparmor_parser -r /etc/apparmor.d/usr.bin.passt
}

try_rules() {
  local label="$1" rules="$2"
  echo "#################### ${label}"
  if ! write_profile "$rules"; then
    echo "${label}: profile failed to load"
    return
  fi
  sudo dmesg --clear || true
  run_probe "$label"
  echo "### ${label}: denial records"
  sudo dmesg | grep -iE "apparmor.*(passt|DENIED)" | tail -10 || echo "(none)"
}

try_rules as_shipped ""
try_rules unix_rule "  unix (accept, bind, connect, listen, receive, send),"
try_rules tmp_rw "  owner /tmp/** rw,"
try_rules network_unix "  network unix,"
try_rules everything "  unix (accept, bind, connect, listen, receive, send),
  owner /tmp/** rw,
  network unix,"
