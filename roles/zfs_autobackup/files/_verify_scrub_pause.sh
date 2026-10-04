#!/bin/bash
set -euo pipefail

scratch=$(mktemp -d /var/tmp/zfs_backup_scan_test.XXXXXX)
scan_suspend=/sys/module/zfs/parameters/zfs_scan_suspend_progress
saved_suspend=$(cat "$scan_suspend")
cleanup() {
  printf '%s\n' "$saved_suspend" >"$scan_suspend"
  # Creation can fail before the pool exists; the backing files still need removal.
  zpool destroy zfs_backup_scan_test 2>/dev/null || true
  rm -f /var/tmp/zfs_backup_scan_test_{0,1}.img
  rm -rf "$scratch"
}
trap cleanup EXIT

# The fixture's self-referential onsite source cannot authenticate; stand in
# for the pull so the real nightly backup can succeed.
mkdir "$scratch/bin" "$scratch/fail"
cat >"$scratch/bin/zfs_backup_onsite" <<'EOF'
#!/bin/bash
set -euo pipefail
echo "Verified onsite pull: $*"
EOF
# Fail the bounded ZFS query that starts with ZFS_VERIFY_FAIL.
cat >"$scratch/fail/timeout" <<'EOF'
#!/bin/bash
set -euo pipefail
if [[ "$*" == "-k 10 60 $ZFS_VERIFY_FAIL"* ]]; then
  echo >&2 "Injected $ZFS_VERIFY_FAIL failure"
  exit 2
fi
exec /usr/bin/timeout "$@"
EOF
chmod +x "$scratch/bin/zfs_backup_onsite" "$scratch/fail/timeout"
export PATH="$scratch/bin:$PATH"

truncate -s 256M /var/tmp/zfs_backup_scan_test_{0,1}.img
zpool create -f zfs_backup_scan_test mirror /var/tmp/zfs_backup_scan_test_{0,1}.img
zfs set autobackup:bak=true zfs_backup_scan_test
dd if=/dev/urandom of=/zfs_backup_scan_test/payload bs=1M count=64 conv=fsync status=none
# Hold real scan state across the backup and its EXIT-trap resume.
printf '1\n' >"$scan_suspend"
zpool scrub zfs_backup_scan_test
[[ $(/opt/zfs/zfs_status.py scan --scrub-only zfs_backup_scan_test) == 1 ]]
output=$(/usr/local/bin/zfs_autosnapshot)
[[ "$output" == *'Paused in-progress scrub on zfs_backup_scan_test for the backup window'* ]]
[[ $(/opt/zfs/zfs_status.py scan --scrub-only zfs_backup_scan_test) == 1 ]]

zpool scrub -p zfs_backup_scan_test
# Snapshot names have one-second precision; exercise another real backup.
sleep 2
output=$(/usr/local/bin/zfs_autosnapshot)
[[ "$output" != *'Paused in-progress scrub on zfs_backup_scan_test for the backup window'* ]]
[[ $(/opt/zfs/zfs_status.py scan --scrub-only zfs_backup_scan_test) == 0 ]]
LC_ALL=C zpool status zfs_backup_scan_test | grep -F 'scrub paused since'
echo 'Backup paused and resumed its running scrub and preserved an already-paused scrub'

# A failed scan query ($1) must be counted without stopping snapshots or pulls.
expect_continued_backup() {
  local before after output
  before=$(zfs list -H -t snapshot -o name zfs_backup_scan_test)
  sleep 2
  if output=$(PATH="$scratch/fail:$PATH" ZFS_VERIFY_FAIL="$1" /usr/local/bin/zfs_autosnapshot 2>&1); then
    echo >&2 "Expected a counted $1 failure"
    exit 1
  fi
  [[ "$output" == *"Injected $1 failure"* ]]
  [[ "$output" == *'Verified onsite pull:'* ]]
  after=$(zfs list -H -t snapshot -o name zfs_backup_scan_test)
  [[ "$after" != "$before" && "$after" == *'@bak-'* ]]
}
expect_continued_backup 'zpool status'
expect_continued_backup 'zpool list -H -o name'
echo 'Real snapshots and peer pulls continued after scan-reader and pool-list failures'
