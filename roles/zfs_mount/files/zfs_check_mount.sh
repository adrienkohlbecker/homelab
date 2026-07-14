#!/bin/bash
set -euo pipefail

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

DATASET=${1:-}
MOUNTPOINT=${2:-}

if [ -z "$DATASET" ] || [ -z "$MOUNTPOINT" ]; then
  echo >&2 "Usage: zfs_check_mount DATASET MOUNTPOINT"
  exit 1
fi

# One query returns every property against a single consistent view of
# the dataset, rather than forking zfs once per property. Boot environments
# stay canmount=noauto so sibling BEs never race to mount; regular datasets
# must remain canmount=on.
if [[ "$DATASET" == rpool/ROOT/* ]]; then
  EXPECTED_CANMOUNT=noauto
else
  EXPECTED_CANMOUNT=on
fi

output=$(zfs get -pH -o property,value type,mounted,mountpoint,readonly,canmount -- "$DATASET") || {
  echo >&2 "Error: failed to query properties on $DATASET"
  exit 1
}

properties=(type mounted mountpoint readonly canmount)
expected=(filesystem yes "$MOUNTPOINT" off "$EXPECTED_CANMOUNT")
actual=()
while IFS=$'\t' read -r _ value; do
  actual+=("$value")
done <<<"$output"

for index in "${!properties[@]}"; do
  if [ "${actual[$index]:-}" != "${expected[$index]}" ]; then
    echo >&2 "Error: $DATASET ${properties[$index]} is '${actual[$index]:-}', expected '${expected[$index]}'"
    exit 1
  fi
done

# Cross-check the live mount table: a single findmnt that matches only when
# DATASET is the zfs source AND it is mounted at MOUNTPOINT. The exit code
# is the whole test -- no separate source string-compare to drift.
findmnt --source "$DATASET" --mountpoint "$MOUNTPOINT" --noheadings --types zfs >/dev/null || {
  echo >&2 "Error: zfs dataset $DATASET is not the mount at $MOUNTPOINT"
  exit 1
}

echo "OK: $DATASET mounted at $MOUNTPOINT (canmount=$EXPECTED_CANMOUNT)"
