#!/bin/bash
set -euo pipefail

backup_script=$1
peer_backup_script=$2
scratch=$(mktemp -d /var/tmp/zfs_backup_scan_test.XXXXXX)
cp -p /opt/zfs/zfs_status.py "$scratch/status.py"
scan_suspend=/sys/module/zfs/parameters/zfs_scan_suspend_progress
saved_suspend=$(cat "$scan_suspend")
cleanup() {
  cp -p "$scratch/status.py" /opt/zfs/zfs_status.py
  printf '%s\n' "$saved_suspend" >"$scan_suspend"
  # Creation can fail before the pool exists; the backing files still need removal.
  zpool destroy zfs_backup_scan_test 2>/dev/null || true
  rm -f /var/tmp/zfs_backup_scan_test_{0,1}.img
  rm -rf "$scratch"
}
trap cleanup EXIT

truncate -s 256M /var/tmp/zfs_backup_scan_test_{0,1}.img
zpool create -f zfs_backup_scan_test mirror /var/tmp/zfs_backup_scan_test_{0,1}.img
zfs set autobackup:bak=true zfs_backup_scan_test
dd if=/dev/urandom of=/zfs_backup_scan_test/payload bs=1M count=64 conv=fsync status=none
# Hold real scan state across the backup and its EXIT-trap resume.
printf '1\n' >"$scan_suspend"
zpool scrub zfs_backup_scan_test
[[ $(/opt/zfs/zfs_status.py scan --scrub-only zfs_backup_scan_test) == 1 ]]
output=$("$backup_script")
[[ "$output" == *'Paused in-progress scrub on zfs_backup_scan_test for the backup window'* ]]
[[ $(/opt/zfs/zfs_status.py scan --scrub-only zfs_backup_scan_test) == 1 ]]

zpool scrub -p zfs_backup_scan_test
# Snapshot names have one-second precision; exercise another real backup.
sleep 2
output=$("$backup_script")
[[ "$output" != *'Paused in-progress scrub on zfs_backup_scan_test for the backup window'* ]]
[[ $(/opt/zfs/zfs_status.py scan --scrub-only zfs_backup_scan_test) == 0 ]]
LC_ALL=C zpool status zfs_backup_scan_test | grep -F 'scrub paused since'
echo 'Backup paused and resumed its running scrub and preserved an already-paused scrub'

cat >/opt/zfs/zfs_status.py <<'EOF'
#!/bin/bash
set -euo pipefail
echo >&2 'Injected scan reader failure'
exit 2
EOF
cat >"$scratch/zfs_backup_onsite" <<'EOF'
#!/bin/bash
set -euo pipefail
echo "Verified onsite pull after scan failure: $*"
EOF
chmod +x "$scratch/zfs_backup_onsite"

before=$(zfs list -H -t snapshot -o name zfs_backup_scan_test)
sleep 2
if output=$(PATH="$scratch:$PATH" "$peer_backup_script" 2>&1); then
  echo >&2 'Expected a counted scan-query failure'
  exit 1
fi
[[ "$output" == *'Injected scan reader failure'* ]]
[[ "$output" == *'Verified onsite pull after scan failure:'* ]]
after=$(zfs list -H -t snapshot -o name zfs_backup_scan_test)
[[ "$after" != "$before" && "$after" == *'@bak-'* ]]

cat >"$scratch/timeout" <<'EOF'
#!/bin/bash
set -euo pipefail
if [[ "$*" == '-k 10 60 zpool list -H -o name' ]]; then
  echo >&2 'Injected pool-list failure'
  exit 124
fi
exec /usr/bin/timeout "$@"
EOF
chmod +x "$scratch/timeout"
before=$after
sleep 2
if output=$(PATH="$scratch:$PATH" "$peer_backup_script" 2>&1); then
  echo >&2 'Expected a counted pool-list failure'
  exit 1
fi
[[ "$output" == *'Injected pool-list failure'* ]]
[[ "$output" == *'Verified onsite pull after scan failure:'* ]]
after=$(zfs list -H -t snapshot -o name zfs_backup_scan_test)
[[ "$after" != "$before" && "$after" == *'@bak-'* ]]
echo 'Real snapshots and peer pulls continued after scan-reader and pool-list failures'
