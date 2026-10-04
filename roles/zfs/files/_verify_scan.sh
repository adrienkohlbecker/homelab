#!/bin/bash
set -euo pipefail

chart_script=$1
scratch=$(mktemp -d /var/tmp/zfs_scan_test.XXXXXX)
cp -p /opt/zfs/zfs_status.py "$scratch/status.py"
scan_suspend=/sys/module/zfs/parameters/zfs_scan_suspend_progress
saved_suspend=$(cat "$scan_suspend")
restore_zed=0
cleanup() {
  cp -p "$scratch/status.py" /opt/zfs/zfs_status.py
  printf '%s\n' "$saved_suspend" >"$scan_suspend"
  # The pool can be absent if creation failed; always remove the backing files.
  zpool destroy zfs_scan_test 2>/dev/null || true
  rm -f /var/tmp/zfs_scan_test_{0,1,2}.img
  if ((restore_zed)); then
    systemctl start zfs-zed
  fi
  rm -rf "$scratch"
}
trap cleanup EXIT

# ZED starts a scrub after resilvers and would overwrite the history under test.
if systemctl is-active --quiet zfs-zed; then
  restore_zed=1
  systemctl stop zfs-zed
fi

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
scrub_date=$(LC_ALL=C zpool status zfs_scan_test | awk '/scan: scrub repaired/ {print $(NF - 4), $(NF - 3), $(NF - 2), $(NF - 1), $NF}')
scrub_epoch=$(date -d "$scrub_date" +%s)
echo 'Active, paused, canceled, completed, resumed, and unprivileged chart checks passed'

sleep 3
printf '1\n' >"$scan_suspend"
zpool offline zfs_scan_test /var/tmp/zfs_scan_test_0.img
dd if=/dev/urandom of=/zfs_scan_test/payload bs=1M count=1 conv=notrunc,fsync status=none
# Commit changed blocks to the remaining mirror leg, beyond the intent log.
timeout -k 10 60 zpool sync zfs_scan_test
zpool online zfs_scan_test /var/tmp/zfs_scan_test_0.img
# Hold the scan until its asynchronous start is observable before waiting for
# completion; an idle wait can otherwise return before a resilver is scheduled.
# shellcheck disable=SC2016 # The substitution runs in the child shell.
timeout -k 10 60 bash -c 'until [[ $(/opt/zfs/zfs_status.py scan zfs_scan_test) == 1 ]]; do sleep 0.1; done'
printf '%s\n' "$saved_suspend" >"$scan_suspend"
timeout -k 10 60 zpool wait -t resilver zfs_scan_test
resilver_status=$(LC_ALL=C zpool status zfs_scan_test)
grep -F 'scan: resilvered' <<<"$resilver_status" || {
  echo >&2 "$resilver_status"
  exit 1
}
resilver_date=$(awk '/scan: resilvered/ {print $(NF - 4), $(NF - 3), $(NF - 2), $(NF - 1), $NF}' <<<"$resilver_status")
resilver_epoch=$(date -d "$resilver_date" +%s)
((resilver_epoch - scrub_epoch >= 3))

# Keep real pool reads, isolate the age assertion, and freeze both parser clocks
# so command latency cannot turn a two-second age threshold into a flaky test.
mkdir "$scratch/bin"
export ZFS_VERIFY_ZPOOL
ZFS_VERIFY_ZPOOL=$(command -v zpool)
cat >"$scratch/bin/zpool" <<'EOF'
#!/bin/bash
set -euo pipefail
case "$*" in
  "list -H -o name") echo zfs_scan_test ;;
  "status -s" | "status -p") exec "$ZFS_VERIFY_ZPOOL" "$@" zfs_scan_test ;;
  *) exec "$ZFS_VERIFY_ZPOOL" "$@" ;;
esac
EOF
cat >"$scratch/bin/date" <<'EOF'
#!/bin/bash
set -euo pipefail
if [[ "$*" == +%s ]]; then
  echo "$ZFS_VERIFY_NOW"
else
  exec /usr/bin/date "$@"
fi
EOF
cat >"$scratch/bin/zfs" <<'EOF'
#!/bin/bash
set -euo pipefail
echo >&2 'Pool creation must not be queried after a completed resilver'
exit 2
EOF
cat >"$scratch/bin/mail" <<'EOF'
#!/bin/bash
set -euo pipefail
cat >"$ZFS_VERIFY_MAIL"
EOF
chmod +x "$scratch/bin/"*
export ZFS_VERIFY_MAIL="$scratch/mail"
check_resilver_age() {
  PATH="$scratch/bin:$PATH" ZFS_VERIFY_NOW="$1" SCRUB_EXPIRE=2 python3 - <<'PY'
import os
import runpy
import sys

reader = runpy.run_path("/opt/zfs/zfs_status.py")
reader["time"].time = lambda: int(os.environ["ZFS_VERIFY_NOW"])
sys.argv = ["zfs_health", "health"]
sys.exit(reader["main"]())
PY
}
check_resilver_age "$((resilver_epoch + 1))"
[[ ! -e "$ZFS_VERIFY_MAIL" ]]
rc=0
output=$(check_resilver_age "$((resilver_epoch + 2))" 2>&1) || rc=$?
if [[ "$rc" != 1 ]]; then
  echo >&2 "Expected a counted expiry failure (exit 1), got $rc: $output"
  exit 1
fi
grep -qF 'ERROR :: Scrub expired on zfs_scan_test' "$ZFS_VERIFY_MAIL"
[[ "$output" == *'Scrub expired on zfs_scan_test'* ]]
[[ "$output" != *'Pool creation must not be queried'* ]]
echo 'Health measured age from the real completed resilver, accepted an available spare, and expired at the threshold'

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
