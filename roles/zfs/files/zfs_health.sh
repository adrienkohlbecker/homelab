#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

EMAIL_TO="root"
EMAIL_SUBJECT_PREFIX="[$(hostname -s)] zfs health"

# Scrub expiration in seconds (40 days)
SCRUB_EXPIRE=3456000

# Pool capacity warning threshold (percent, without the % sign)
CAPACITY_WARN=80

TMP_OUTPUT=$(mktemp)
trap 'rm -f "$TMP_OUTPUT"' EXIT

ERRORS=0

zpool status | tee "$TMP_OUTPUT"

ZFS_VOLUMES=$(zpool list -H -o name)

# Health — use the authoritative pool-level health property rather than
# grepping keywords, so any future failure state is automatically caught.
echo "Checking pool health condition..."
while IFS=$'\t' read -r pool health; do
  if [ "$health" != "ONLINE" ]; then
    echo >&2 "ERROR :: Pool $pool is $health"
    ERRORS=$((ERRORS + 1))
  fi
done < <(zpool list -H -o name,health)

# Sub-vdev check — catches OFFLINE/DEGRADED individual disks that the
# pool-level health may already reflect, but also REMOVED/corrupt/cannot
# states that only appear in zpool status output.
if grep -q -e 'DEGRADED\|FAULTED\|OFFLINE\|UNAVAIL\|REMOVED\|FAIL\|DESTROYED\|corrupt\|cannot\|unrecover' "$TMP_OUTPUT"; then
  echo >&2 "ERROR :: Detected sub-vdev or device fault in zpool status"
  ERRORS=$((ERRORS + 1))
fi

# Drive errors — use -p for exact numeric values (human-formatted output
# shows 1.5K/2M for large counts, which the old concatenate-and-grep missed).
echo "Checking drive errors..."
if zpool status -p | awk '/ONLINE/ && !/state/ { if ($3+0 + $4+0 + $5+0 > 0) { found=1 } } END { exit !found }'; then
  echo >&2 "ERROR :: Detected drive errors (READ/WRITE/CKSUM)"
  ERRORS=$((ERRORS + 1))
fi

# Capacity — ZFS performance degrades above ~80% due to COW fragmentation.
echo "Checking pool capacity..."
while IFS=$'\t' read -r pool cap_str; do
  cap=${cap_str%%%}
  if [ "$cap" -ge "$CAPACITY_WARN" ]; then
    echo >&2 "ERROR :: Pool $pool is ${cap}% full (threshold: ${CAPACITY_WARN}%)"
    ERRORS=$((ERRORS + 1))
  fi
done < <(zpool list -H -o name,capacity)

# Scrub age — check each volume independently.
echo "Checking scrub age..."
CURRENT_DATE=$(date +"%s")

for volume in $ZFS_VOLUMES; do
  vol_status=$(zpool status "$volume")

  if echo "$vol_status" | grep -q -e "scrub canceled"; then
    echo >&2 "ERROR :: Last scrub canceled on $volume"
    ERRORS=$((ERRORS + 1))
    continue
  elif echo "$vol_status" | grep -q -e "scrub in progress\|resilver"; then
    echo "Scrub in progress for $volume, skipping."
    continue
  fi

  if (! echo "$vol_status" | grep -q -e "scan: scrub") || (echo "$vol_status" | grep -q "none requested"); then
    SCRUB_DATE=$(zfs get creation -Hpo value "$volume")
  else
    SCRUB_RAW_DATE=$(echo "$vol_status" | grep -e "scrub repaired" -e "scrub paused" | rev | cut -d' ' -f1-5 | rev)
    SCRUB_DATE=$(date -d "$SCRUB_RAW_DATE" +"%s" 2>/dev/null) || {
      echo >&2 "ERROR :: Cannot parse scrub date for $volume: $SCRUB_RAW_DATE"
      ERRORS=$((ERRORS + 1))
      continue
    }
  fi

  if [ $((CURRENT_DATE - SCRUB_DATE)) -ge $SCRUB_EXPIRE ]; then
    echo >&2 "ERROR :: Scrub expired on $volume"
    ERRORS=$((ERRORS + 1))
  fi
done

if [ "$ERRORS" -gt 0 ]; then
  mail -s "$EMAIL_SUBJECT_PREFIX - $ERRORS issue(s) detected" "$EMAIL_TO" <"$TMP_OUTPUT"
  exit 1
fi

echo "Done"
