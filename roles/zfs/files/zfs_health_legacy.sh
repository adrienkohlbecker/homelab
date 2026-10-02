#!/bin/bash
set -euo pipefail
((EUID == 0)) || {
  echo >&2 "Error: I require root"
  exit 1
}

# The checks below match zpool prose and parse its dates; pin the locale as
# zfs_status.py does for its own queries.
export LC_ALL=C

# zpool status issues blocking I/O; on a SUSPENDED pool (too many devices lost,
# all I/O wedged) it can hang indefinitely and stall the nightly timer. Bound
# every call so a wedged pool surfaces as a counted failure plus email the same
# night instead of a silent hang. 60s is far longer than a healthy status read
# yet well inside the daily cadence. -k 10 escalates to SIGKILL 10s after the
# SIGTERM if zpool ignores the term; a process stuck in uninterruptible I/O wait
# can still outlast it, but the unit's systemd timeout remains the final backstop.
zpool_status() {
  timeout -k 10 60 zpool status "$@"
}

EMAIL_TO="root"
EMAIL_SUBJECT_PREFIX="[$(hostname -s)] zfs health"

# Scrub expiration in seconds (40 days). Scrubs themselves are scheduled by the
# zfs_scrub timer (monthly, second Sunday), not run here; this role also diverts
# the distro's /etc/cron.d/zfsutils-linux aside so that timer is the sole
# scheduler. The watchdog measures age since the latest completed scrub,
# resilver/rebuild, or paused scrub, with pool creation as the fallback.
# Forty days allows one monthly cycle plus slack. Overridable via the
# environment so the _verify harness can force the expiry branch
# (SCRUB_EXPIRE=1) without faking a scrub date or waiting 40 days; prod always
# takes the default.
SCRUB_EXPIRE="${SCRUB_EXPIRE:-3456000}"

# Pool capacity is alarmed by netdata's zfspool collector (per-pool
# netdata_zfspool_thresholds with escalating warn/crit), not duplicated here.

failed=0
TMP_OUTPUT=$(mktemp)
ERRORS=$(mktemp)
trap 'rm -f "$TMP_OUTPUT" "$ERRORS"' EXIT

# Keep each failure for the mail, matching zfs_status.py finish_health: the
# ERROR lines come first, then the zpool status report.
fail() {
  printf 'ERROR :: %s\n' "$1" | tee -a "$ERRORS" >&2
  ((failed += 1))
}

zpool_status -s | tee "$TMP_OUTPUT" || echo "Warning: zpool status report did not complete" | tee -a "$TMP_OUTPUT" >&2

if ! ZFS_VOLUMES=$(timeout -k 10 60 zpool list -H -o name); then
  fail "Cannot list pools"
  ZFS_VOLUMES=
fi

# Health — `zpool status -x` is the authoritative summary: it prints exactly
# "all pools are healthy" when every imported pool is ONLINE with no known
# errors, and the full status of any pool that is not. Trusting it (rather than
# grepping zpool status for a hand-maintained keyword blocklist) catches any
# future fault string automatically and stops pool/dataset names that happen to
# contain words like "cannot" or "fail" from false-positiving.
echo "Checking pool health condition..."
if ! health_summary=$(zpool_status -x); then
  fail "zpool status -x did not complete (pool wedged or zpool error)"
elif [ "$health_summary" != "all pools are healthy" ]; then
  fail "zpool status -x reports a problem:"$'\n'"$health_summary"
fi

# Drive errors — count READ/WRITE/CKSUM on every row whose last three columns
# are integers, regardless of the row's STATE: a disk can rack up errors and
# then flip to DEGRADED/FAULTED, and that error count is exactly what we want to
# surface (the -x check above reports the state; this reports the counts). The
# awk's own `> 0` guard already ignores healthy 0/0/0 rows, so we deliberately
# do NOT pass `-e` (errored-vdevs-only): that flag is OpenZFS 2.3+, and on a 2.2
# host `zpool status -e` aborts with "invalid option", which under pipefail
# fails the pipe and silently kills the whole check. -p forces exact integers
# (human-formatted 1.5K/2M would slip past the > 0 test). Capture into a var
# first (rather than piping zpool straight into awk) so a timeout/zpool failure
# is caught here instead of being masked by awk's own exit status under pipefail.
echo "Checking drive errors..."
if ! drive_status=$(zpool_status -p); then
  fail "zpool status -p did not complete (pool wedged or zpool error)"
elif echo "$drive_status" | awk '$3 ~ /^[0-9]+$/ && $4 ~ /^[0-9]+$/ && $5 ~ /^[0-9]+$/ { if ($3 + $4 + $5 > 0) found = 1 } END { exit !found }'; then
  fail "Detected drive errors (READ/WRITE/CKSUM)"
fi

# Scrub age — check each volume independently.
echo "Checking scrub age..."
CURRENT_DATE=$(date +"%s")

for volume in $ZFS_VOLUMES; do
  # A suspended/UNAVAIL pool can make `zpool status` exit non-zero. Count it and
  # keep going so a broken pool does not swallow the rest of the alert.
  vol_status=$(zpool_status "$volume") || {
    fail "Cannot query status for $volume"
    continue
  }

  if [[ "$vol_status" == *"scrub canceled"* ]]; then
    fail "Last scrub canceled on $volume"
    continue
  elif [[ "$vol_status" == *"scrub in progress"* ]] || grep -Eq 'resilver( \([^)]*\))? in progress' <<<"$vol_status"; then
    echo "Scrub/resilver in progress for $volume, skipping."
    continue
  fi

  SCRUB_RAW_DATES=$(awk '/scan: (scrub repaired|scrub paused|resilvered)/ {print $(NF - 4), $(NF - 3), $(NF - 2), $(NF - 1), $NF}' <<<"$vol_status")
  age_basis=""
  if [[ -z "$SCRUB_RAW_DATES" ]]; then
    SCRUB_DATE=$(timeout -k 10 60 zfs get creation -Hpo value "$volume") || {
      fail "Cannot check scrub age for $volume"
      continue
    }
    age_basis=" (age since pool creation; no usable scan timestamp)"
  else
    # Sequential rebuilds add one scan line per vdev alongside scrub history.
    # Parse every trailing ctime date and select the latest valid timestamp.
    SCRUB_DATE=0
    while IFS= read -r raw_date; do
      scan_date=$(date -d "$raw_date" +"%s" 2>/dev/null) || {
        fail "Cannot parse scrub date for $volume: $raw_date"
        continue 2
      }
      if ((scan_date > SCRUB_DATE)); then
        SCRUB_DATE=$scan_date
      fi
    done <<<"$SCRUB_RAW_DATES"
  fi

  if [ $((CURRENT_DATE - SCRUB_DATE)) -ge "$SCRUB_EXPIRE" ]; then
    fail "Scrub expired on $volume$age_basis"
  fi
done

if ((failed > 0)); then
  # `mail` comes from the postfix role, converged earlier in the layer ladder.
  # The _verify dry-run never reaches this branch because clean fixture pools
  # report no failures.
  cat "$ERRORS" "$TMP_OUTPUT" | mail -s "$EMAIL_SUBJECT_PREFIX - $failed issue(s) detected" "$EMAIL_TO"
  exit 1
fi

echo "Done"
