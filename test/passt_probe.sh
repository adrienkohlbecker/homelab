#!/usr/bin/env bash
# Temporary investigation helper: does AppArmor confinement explain passt
# failing on the AWS qemu hosts while working on lab? Runs passt twice against
# a throwaway unix socket, once as shipped and once with the profile in
# complain mode, and prints what each run logged plus any kernel denials.
set -euo pipefail

sock=/tmp/passt_probe.sock
log=/tmp/passt_probe.log

client_py=$(
	cat <<'PY'
import socket, struct, sys

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sys.argv[1])
# Broadcast ARP request for an address passt owns; a healthy passt answers it.
frame = (
    b"\xff" * 6
    + b"\x52\x54\x00\x12\x34\x56"
    + b"\x08\x06"
    + b"\x00\x01\x08\x00\x06\x04\x00\x01"
    + b"\x52\x54\x00\x12\x34\x56"
    + bytes([169, 254, 0, 2])
    + b"\x00" * 6
    + bytes([169, 254, 0, 1])
)
s.sendall(struct.pack(">I", len(frame)) + frame)
s.settimeout(3)
try:
    print("client: got %d bytes back" % len(s.recv(65536)))
except Exception as exc:  # noqa: BLE001 - probe reports whatever went wrong
    print("client: no reply (%s)" % exc)
PY
)

run_probe() {
	local label="$1"
	rm -f "$sock" "$log"
	dmesg --clear 2>/dev/null || sudo dmesg --clear || true

	passt --socket "$sock" --foreground --trace >"$log" 2>&1 &
	local pid=$!
	for _ in $(seq 20); do
		[ -S "$sock" ] && break
		sleep 0.25
	done

	python3 -c "$client_py" "$sock" || true
	sleep 2
	kill "$pid" 2>/dev/null || true
	wait "$pid" 2>/dev/null || true

	echo "=== ${label}: accepted connection? ==="
	grep -c "accepted connection from PID" "$log" || true
	echo "=== ${label}: send failures ==="
	grep -c "failed to send" "$log" || true
	echo "=== ${label}: log tail ==="
	tail -40 "$log"
	echo "=== ${label}: kernel denials ==="
	(dmesg 2>/dev/null || sudo dmesg) | grep -iE "apparmor|denied" | tail -20 || true
}

echo "### passt profile state"
sudo aa-status 2>&1 | grep -iE "passt|userns" || echo "(no passt profile listed)"
ls -l /etc/apparmor.d/*passt* 2>/dev/null || echo "(no passt profile file)"

run_probe "confined"

echo "### switching the passt profile to complain mode"
sudo aa-complain /usr/bin/passt || sudo aa-complain passt || echo "(aa-complain failed)"

run_probe "complain"
