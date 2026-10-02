#!/bin/bash
set -euo pipefail

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
mkdir "$scratch/bin"
cat >"$scratch/bin/zpool" <<'EOF'
#!/bin/bash
set -euo pipefail
case "$*" in
  "list -H -o name")
    [[ -z "${ZFS_VERIFY_LIST_FAIL:-}" ]] || exit 2
    echo tank
    ;;
  "status -x") echo 'all pools are healthy' ;;
  "status "*) cat "$ZFS_VERIFY_STATUS" ;;
  *) exit 2 ;;
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
if [[ -n "${ZFS_VERIFY_CREATION_FAIL:-}" ]]; then
  echo >&2 'Injected creation query failure'
  exit 2
fi
if [[ -n "${ZFS_VERIFY_CREATION:-}" ]]; then
  echo "$ZFS_VERIFY_CREATION"
  exit 0
fi
echo >&2 'Unexpected pool-creation fallback'
exit 2
EOF
cat >"$scratch/bin/mail" <<'EOF'
#!/bin/bash
set -euo pipefail
cat >"$ZFS_VERIFY_MAIL"
EOF
chmod +x "$scratch/bin/"*
export ZFS_VERIFY_STATUS="$scratch/status" ZFS_VERIFY_MAIL="$scratch/mail" ZFS_VERIFY_NOW=1700000105
old_date=$(LC_ALL=C date -d @1700000000 '+%a %b %e %T %Y')
recent_date=$(LC_ALL=C date -d @1700000100 '+%a %b %e %T %Y')
middle_date=$(LC_ALL=C date -d @1700000050 '+%a %b %e %T %Y')

check_status() {
  local expected=$1 output rc=0
  printf '%s\n' "$2" >"$ZFS_VERIFY_STATUS"
  rm -f "$ZFS_VERIFY_MAIL"
  output=$(PATH="$scratch/bin:$PATH" LC_ALL=C SCRUB_EXPIRE=10 /opt/zfs/zfs_health_legacy.sh 2>&1) || rc=$?
  [[ "$rc" == "$expected" ]] || {
    echo >&2 "$output"
    return 1
  }
  [[ "$output" != *'Unexpected pool-creation fallback'* ]]
  if [[ "$expected" == 0 ]]; then
    [[ "$output" == *Done* ]]
    [[ ! -e "$ZFS_VERIFY_MAIL" ]]
  else
    [[ "$output" == *"$3"* ]]
    # The mail carries the ERROR lines ahead of the status report.
    grep -qF -- "ERROR :: " "$ZFS_VERIFY_MAIL" || {
      echo >&2 'Mail lacks the ERROR lines:'
      cat >&2 "$ZFS_VERIFY_MAIL"
      return 1
    }
    grep -qF -- "$3" "$ZFS_VERIFY_MAIL"
  fi
}

check_status 0 "scan: resilvered 0B in 00:00:01 with 0 errors on $recent_date"
check_status 0 "scan: resilver (mirror-0) in progress since $recent_date"
check_status 0 "scan: scrub repaired 0B in 00:00:01 with 0 errors on $old_date
scan: resilver (mirror-0) in progress since $recent_date"
check_status 0 "scan: scrub repaired 0B in 00:00:01 with 0 errors on $old_date
scan: resilvered (mirror-0) 64M in 00:00:01 with 0 errors on $recent_date
scan: resilvered (mirror-1) 64M in 00:00:01 with 0 errors on $middle_date"
check_status 0 "scan: scrub repaired 0B in 00:00:01 with 0 errors on $recent_date
scan: resilvered (mirror-0) 64M in 00:00:01 with 0 errors on $old_date"
check_status 0 "scan: scrub paused since $recent_date
scan: resilvered (mirror-0) 64M in 00:00:01 with 0 errors on $old_date"
ZFS_VERIFY_NOW=1700000110 check_status 1 "scan: resilvered 0B in 00:00:01 with 0 errors on $recent_date" 'Scrub expired on tank'
check_status 1 "scan: scrub repaired 0B in 00:00:01 with 0 errors on $recent_date
scan: resilvered (mirror-0) 64M in 00:00:01 with 0 errors on an invalid date" 'Cannot parse scrub date for tank'
ZFS_VERIFY_CREATION=1700000000 check_status 1 'scan: none requested' 'age since pool creation; no usable scan timestamp'
ZFS_VERIFY_CREATION=1700000000 check_status 1 "scan: resilver canceled on $recent_date" 'age since pool creation; no usable scan timestamp'
ZFS_VERIFY_LIST_FAIL=1 check_status 1 "scan: scrub repaired 0B in 00:00:01 with 0 errors on $recent_date" 'Cannot list pools'
ZFS_VERIFY_CREATION_FAIL=1 check_status 1 'scan: none requested' 'Cannot check scrub age for tank'
echo 'Legacy scan dates select the newest scrub, resilver, rebuild, or pause and reject malformed dates; failed pool and creation queries are counted and mailed'
