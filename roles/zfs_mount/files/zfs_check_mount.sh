#!/bin/bash
set -euo pipefail
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

DATASET=${1:-}
MOUNTPOINT=${2:-}

if [ -z "$DATASET" ] || [ -z "$MOUNTPOINT" ]; then
  echo >&2 "Usage: zfs_check_mount DATASET MOUNTPOINT"
  exit 1
fi

f_check_property() {
  local property=$1 expected=$2
  local actual
  actual=$(zfs get -pH -o value "$property" "$DATASET") || {
    echo >&2 "Error: failed to query $property on $DATASET"
    exit 1
  }
  if [ "$actual" != "$expected" ]; then
    echo >&2 "Error: $DATASET $property is '$actual', expected '$expected'"
    exit 1
  fi
}

f_check_property type filesystem
f_check_property mounted yes
f_check_property mountpoint "$MOUNTPOINT"
f_check_property readonly off
f_check_property canmount on

SOURCE=$(findmnt --first-only --mountpoint "$MOUNTPOINT" --noheadings --types zfs --output SOURCE) || {
  echo >&2 "Error: no zfs mount found at $MOUNTPOINT"
  exit 1
}
if [ "$SOURCE" != "$DATASET" ]; then
  echo >&2 "Error: zfs mount at $MOUNTPOINT is '$SOURCE', expected '$DATASET'"
  exit 1
fi
