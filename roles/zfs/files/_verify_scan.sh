#!/bin/bash
set -euo pipefail

chart_script=$1
scratch=$(mktemp -d /var/tmp/zfs_scan_test.XXXXXX)
cp -p /opt/zfs/zfs_status.py "$scratch/status.py"
scan_suspend=/sys/module/zfs/parameters/zfs_scan_suspend_progress
saved_suspend=$(cat "$scan_suspend")
cleanup() {
  cp -p "$scratch/status.py" /opt/zfs/zfs_status.py
  printf '%s\n' "$saved_suspend" >"$scan_suspend"
  # The pool can be absent if creation failed; always remove the backing files.
  zpool destroy zfs_scan_test 2>/dev/null || true
  rm -f /var/tmp/zfs_scan_test_{0,1,2}.img
  rm -rf "$scratch"
}
trap cleanup EXIT

for pool in $(zpool list -H -o name); do
  timeout -k 10 60 zpool wait -t scrub,resilver "$pool"
done
truncate -s 256M /var/tmp/zfs_scan_test_{0,1,2}.img
zpool create -f zfs_scan_test mirror /var/tmp/zfs_scan_test_{0,1}.img spare /var/tmp/zfs_scan_test_2.img
dd if=/dev/urandom of=/zfs_scan_test/payload bs=1M count=64 conv=fsync status=none

check_chart() {
  local expected=$1 output
  # Variables inside the command are expanded by the unprivileged child shell.
  # shellcheck disable=SC2016
  output=$(runuser -u nobody -- bash -c '
    source "$1"
    error() { echo >&2 "$*"; }
    zfs_scan_check
    zfs_scan_update 0
  ' bash "$chart_script")
  [[ "$output" == *"SET active = $expected"* ]]
}

# Hold real scan state long enough to test it without racing a tiny scrub.
printf '1\n' >"$scan_suspend"
zpool scrub zfs_scan_test
[[ $(/opt/zfs/zfs_status.py scan zfs_scan_test) == 1 ]]
[[ $(/opt/zfs/zfs_status.py scan --scrub-only zfs_scan_test) == 1 ]]
check_chart 1
ZFS_SCRUB_STAGGER_SEC=0 zfs_scrub | grep -F 'Scrub/resilver already running on zfs_scan_test, skipping.'

zpool scrub -p zfs_scan_test
[[ $(/opt/zfs/zfs_status.py scan zfs_scan_test) == 0 ]]
# The scheduler started other pools; pause them so the global chart can be 0.
for pool in $(zpool list -H -o name); do
  if [[ $(/opt/zfs/zfs_status.py scan --scrub-only "$pool") == 1 ]]; then
    zpool scrub -p "$pool"
  fi
done
check_chart 0
ZFS_SCRUB_STAGGER_SEC=0 zfs_scrub >/dev/null
[[ $(/opt/zfs/zfs_status.py scan zfs_scan_test) == 1 ]]

zpool scrub -s zfs_scan_test
[[ $(/opt/zfs/zfs_status.py scan zfs_scan_test) == 0 ]]
printf '%s\n' "$saved_suspend" >"$scan_suspend"
timeout -k 10 60 zpool scrub -w zfs_scan_test
[[ $(/opt/zfs/zfs_status.py scan zfs_scan_test) == 0 ]]
echo 'Active, paused, canceled, completed, resumed, and unprivileged chart checks passed'

zpool offline zfs_scan_test /var/tmp/zfs_scan_test_0.img
dd if=/dev/urandom of=/zfs_scan_test/payload bs=1M count=1 conv=notrunc,fsync status=none
zpool online zfs_scan_test /var/tmp/zfs_scan_test_0.img
timeout -k 10 60 zpool wait -t resilver zfs_scan_test
LC_ALL=C zpool status zfs_scan_test | grep -F 'scan: resilvered'
/usr/local/bin/zfs_health
echo 'Health accepted a real completed resilver and an available spare'

bad_pool=$(zpool list -H -o name | awk '$0 != "zfs_scan_test" {print; exit}')
[[ -n "$bad_pool" ]]
cat >/opt/zfs/zfs_status.py <<'EOF'
#!/bin/bash
set -euo pipefail
if [[ "$2" == "$ZFS_VERIFY_BAD_POOL" ]]; then
  echo >&2 "Injected status failure on $2"
  exit 2
fi
exec "$ZFS_VERIFY_STATUS_READER" "$@"
EOF
if output=$(ZFS_VERIFY_BAD_POOL="$bad_pool" ZFS_VERIFY_STATUS_READER="$scratch/status.py" ZFS_SCRUB_STAGGER_SEC=0 zfs_scrub 2>&1); then
  echo >&2 'Expected a counted per-pool status failure'
  exit 1
fi
[[ "$output" == *"Injected status failure on $bad_pool"* ]]
[[ "$output" == *'Starting scrub on zfs_scan_test'* ]]
echo 'The scheduler scrubbed another pool after a per-pool status failure'
