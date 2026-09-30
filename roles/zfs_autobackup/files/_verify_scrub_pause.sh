#!/bin/bash
set -euo pipefail

backup_script=$1
scan_suspend=/sys/module/zfs/parameters/zfs_scan_suspend_progress
saved_suspend=$(cat "$scan_suspend")
cleanup() {
  printf '%s\n' "$saved_suspend" >"$scan_suspend"
  # Creation can fail before the pool exists; the backing files still need removal.
  zpool destroy zfs_backup_scan_test 2>/dev/null || true
  rm -f /var/tmp/zfs_backup_scan_test_{0,1}.img
}
trap cleanup EXIT

truncate -s 256M /var/tmp/zfs_backup_scan_test_{0,1}.img
zpool create -f zfs_backup_scan_test mirror /var/tmp/zfs_backup_scan_test_{0,1}.img
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
