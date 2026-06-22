#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

DATASET=${1:-}
MOUNTPOINT=${2:-}
EXPECTED_CANMOUNT=${3:-on}

if [ -z "$DATASET" ] || [ -z "$MOUNTPOINT" ]; then
  echo >&2 "Usage: zfs_check_mount DATASET MOUNTPOINT [EXPECTED_CANMOUNT]"
  exit 1
fi

# One query returns every property against a single consistent view of
# the dataset, rather than forking zfs once per property. The expected
# canmount differs by dataset class: regular datasets are canmount=on,
# while boot environments stay noauto so sibling BEs never race to mount.
# The caller passes the expected value (default on).
output=$(zfs get -pH -o property,value type,mounted,mountpoint,readonly,canmount -- "$DATASET") || {
  echo >&2 "Error: failed to query properties on $DATASET"
  exit 1
}

declare -A prop
while IFS=$'\t' read -r name value; do
  prop[$name]=$value
done <<<"$output"

check_property() {
  local property=$1 expected=$2
  local actual=${prop[$property]:-}
  if [ "$actual" != "$expected" ]; then
    echo >&2 "Error: $DATASET $property is '$actual', expected '$expected'"
    exit 1
  fi
}

check_property type filesystem
check_property mounted yes
check_property mountpoint "$MOUNTPOINT"
check_property readonly off
check_property canmount "$EXPECTED_CANMOUNT"

# Cross-check the live mount table: a single findmnt that matches only when
# DATASET is the zfs source AND it is mounted at MOUNTPOINT. The exit code
# is the whole test -- no separate source string-compare to drift.
findmnt --source "$DATASET" --mountpoint "$MOUNTPOINT" --noheadings --types zfs >/dev/null || {
  echo >&2 "Error: zfs dataset $DATASET is not the mount at $MOUNTPOINT"
  exit 1
}

echo "OK: $DATASET mounted at $MOUNTPOINT (canmount=$EXPECTED_CANMOUNT)"
