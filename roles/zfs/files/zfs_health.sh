#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

EMAIL_TO="root"
EMAIL_SUBJECT_PREFIX="[$(hostname -s)] zfs health"

# Scrub expiration in seconds (40 days). Scrubs themselves are scheduled by the
# zfs_scrub timer (monthly, second Sunday), not run here; this role also diverts
# the distro's /etc/cron.d/zfsutils-linux aside so that timer is the sole
# scheduler. This threshold is the watchdog -- it alarms if that monthly scrub
# stops happening (40 days = one monthly cycle plus slack).
SCRUB_EXPIRE=3456000

# Pool capacity is alarmed by netdata's zfspool collector (per-pool
# netdata_zfspool_thresholds with escalating warn/crit), not duplicated here.

# f_failed is the shared error counter declared by functions.sh; we bump it in
# each check's failure branch and propagate non-zero via mail + exit 1 at the
# end -- the loop-isolate-propagate shape the other operator scripts use.
TMP_OUTPUT=$(mktemp)
trap 'rm -f "$TMP_OUTPUT"' EXIT

zpool status | tee "$TMP_OUTPUT"

ZFS_VOLUMES=$(zpool list -H -o name)

# Health — `zpool status -x` is the authoritative summary: it prints exactly
# "all pools are healthy" when every imported pool is ONLINE with no known
# errors, and the full status of any pool that is not. Trusting it (rather than
# grepping zpool status for a hand-maintained keyword blocklist) catches any
# future fault string automatically and stops pool/dataset names that happen to
# contain words like "cannot" or "fail" from false-positiving.
echo "Checking pool health condition..."
health_summary=$(zpool status -x)
if [ "$health_summary" != "all pools are healthy" ]; then
  echo >&2 "ERROR :: zpool status -x reports a problem:"
  echo >&2 "$health_summary"
  ((f_failed += 1))
fi

# Drive errors — count READ/WRITE/CKSUM on every row whose last three columns
# are integers, regardless of the row's STATE: a disk can rack up errors and
# then flip to DEGRADED/FAULTED, and that error count is exactly what we want to
# surface (the -x check above reports the state; this reports the counts). -e
# restricts the output to unhealthy/errored vdevs so a healthy 0/0/0 row can
# never reach the awk; -p forces exact integers (human-formatted 1.5K/2M would
# slip past the > 0 test).
echo "Checking drive errors..."
if zpool status -ep | awk '$3 ~ /^[0-9]+$/ && $4 ~ /^[0-9]+$/ && $5 ~ /^[0-9]+$/ { if ($3 + $4 + $5 > 0) found = 1 } END { exit !found }'; then
  echo >&2 "ERROR :: Detected drive errors (READ/WRITE/CKSUM)"
  ((f_failed += 1))
fi

# Scrub age — check each volume independently.
echo "Checking scrub age..."
CURRENT_DATE=$(date +"%s")

for volume in $ZFS_VOLUMES; do
  # A suspended/UNAVAIL pool can make `zpool status` exit non-zero, which would
  # abort the whole run via the errexit inherited from functions.sh, silently
  # swallowing the alert exactly when a pool is broken. Count it and keep going.
  vol_status=$(zpool status "$volume") || {
    echo >&2 "ERROR :: Cannot query status for $volume"
    ((f_failed += 1))
    continue
  }

  if echo "$vol_status" | grep -q -e "scrub canceled"; then
    echo >&2 "ERROR :: Last scrub canceled on $volume"
    ((f_failed += 1))
    continue
  elif echo "$vol_status" | grep -q -e "scrub in progress\|resilver"; then
    echo "Scrub in progress for $volume, skipping."
    continue
  fi

  if (! echo "$vol_status" | grep -q -e "scan: scrub") || (echo "$vol_status" | grep -q "none requested"); then
    SCRUB_DATE=$(zfs get creation -Hpo value "$volume")
  else
    # Take the trailing 5 fields by position from the end ("Sun Mar 10 03:12:34
    # 2024"), independent of day-width padding and of however many words precede
    # the date on the scan line. A positional `cut` from the front would shift
    # whenever the wording or column count changes.
    SCRUB_RAW_DATE=$(echo "$vol_status" | grep -e "scrub repaired" -e "scrub paused" | awk '{print $(NF - 4), $(NF - 3), $(NF - 2), $(NF - 1), $NF}')
    SCRUB_DATE=$(date -d "$SCRUB_RAW_DATE" +"%s" 2>/dev/null) || {
      echo >&2 "ERROR :: Cannot parse scrub date for $volume: $SCRUB_RAW_DATE"
      ((f_failed += 1))
      continue
    }
  fi

  if [ $((CURRENT_DATE - SCRUB_DATE)) -ge $SCRUB_EXPIRE ]; then
    echo >&2 "ERROR :: Scrub expired on $volume"
    ((f_failed += 1))
  fi
done

if [ "$f_failed" -gt 0 ]; then
  # `mail` comes from the postfix role, converged earlier in the layer ladder.
  # The _verify dry-run never reaches this branch because clean fixture pools
  # report f_failed=0.
  mail -s "$EMAIL_SUBJECT_PREFIX - $f_failed issue(s) detected" "$EMAIL_TO" <"$TMP_OUTPUT"
  exit 1
fi

echo "Done"
