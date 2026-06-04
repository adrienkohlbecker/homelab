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
# canmount differs by dataset class: regular datasets are canmount=on
# (they auto-mount at boot), while boot environments (rpool/ROOT/<release>
# at / and bpool/BOOT/<release> at /boot) stay noauto so sibling BEs never
# race to mount -- the bootloader (ZFSBootMenu) mounts the chosen BE
# explicitly. The caller passes the expected value (default on).
output=$(zfs get -pH -o property,value type,mounted,mountpoint,readonly,canmount -- "$DATASET") || {
  echo >&2 "Error: failed to query properties on $DATASET"
  exit 1
}

declare -A prop
while IFS=$'\t' read -r name value; do
  prop[$name]=$value
done <<< "$output"

f_check_property() {
  local property=$1 expected=$2
  local actual=${prop[$property]:-}
  if [ "$actual" != "$expected" ]; then
    echo >&2 "Error: $DATASET $property is '$actual', expected '$expected'"
    exit 1
  fi
}

f_check_property type filesystem
f_check_property mounted yes
f_check_property mountpoint "$MOUNTPOINT"
f_check_property readonly off
f_check_property canmount "$EXPECTED_CANMOUNT"

SOURCE=$(findmnt --first-only --mountpoint "$MOUNTPOINT" --noheadings --types zfs --output SOURCE) || {
  echo >&2 "Error: no zfs mount found at $MOUNTPOINT"
  exit 1
}
if [ "$SOURCE" != "$DATASET" ]; then
  echo >&2 "Error: zfs mount at $MOUNTPOINT is '$SOURCE', expected '$DATASET'"
  exit 1
fi

echo "OK: $DATASET mounted at $MOUNTPOINT (canmount=$EXPECTED_CANMOUNT)"
