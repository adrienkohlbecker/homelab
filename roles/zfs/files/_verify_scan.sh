#!/bin/bash
set -euo pipefail

scratch=$(mktemp -d /var/tmp/zfs_scan_test.XXXXXX)
scan_suspend=/sys/module/zfs/parameters/zfs_scan_suspend_progress
saved_suspend=$(cat "$scan_suspend")
cleanup() {
  printf '%s\n' "$saved_suspend" >"$scan_suspend"
  # The pool can be absent if creation failed; always remove the backing files.
  zpool destroy zfs_scan_test 2>/dev/null || true
  rm -f /var/tmp/zfs_scan_test_{0,1}.img
  rm -rf "$scratch"
}
trap cleanup EXIT

truncate -s 256M /var/tmp/zfs_scan_test_{0,1}.img
zpool create -f zfs_scan_test mirror /var/tmp/zfs_scan_test_{0,1}.img
dd if=/dev/urandom of=/zfs_scan_test/payload bs=1M count=64 conv=fsync status=none

# The netdata collector runs the reader unprivileged.
scan_state() {
  runuser -u nobody -- /opt/zfs/zfs_status.py scan zfs_scan_test
}

# Confine the scheduler to the test pool while keeping real pool reads.
mkdir "$scratch/bin"
export ZFS_VERIFY_ZPOOL
ZFS_VERIFY_ZPOOL=$(command -v zpool)
cat >"$scratch/bin/zpool" <<'EOF'
#!/bin/bash
set -euo pipefail
if [[ "$*" == "list -H -o name" ]]; then
  echo zfs_scan_test
else
  exec "$ZFS_VERIFY_ZPOOL" "$@"
fi
EOF
chmod +x "$scratch/bin/zpool"
scrub_test_pool() {
  PATH="$scratch/bin:$PATH" ZFS_SCRUB_STAGGER_SEC=0 zfs_scrub
}

# Hold real scan state long enough to test it without racing a tiny scrub.
printf '1\n' >"$scan_suspend"
zpool scrub zfs_scan_test
[[ $(scan_state) == 1 ]]
scrub_test_pool | grep -F 'Scrub/resilver already running on zfs_scan_test, skipping.'

zpool scrub -s zfs_scan_test
[[ $(scan_state) == 0 ]]
printf '%s\n' "$saved_suspend" >"$scan_suspend"
timeout -k 10 60 zpool scrub -w zfs_scan_test
[[ $(scan_state) == 0 ]]
echo 'Active, canceled, and completed scan states read back unprivileged'
