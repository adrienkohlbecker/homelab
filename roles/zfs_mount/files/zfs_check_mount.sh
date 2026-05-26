#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

DATASET=${1:-}
MOUNTPOINT=${2:-}

if [ -z "$DATASET" ] || [ -z "$MOUNTPOINT" ]; then
  echo >&2 "Usage: zfs_check_mount DATASET MOUNTPOINT"
  exit 1
fi

OUTPUT=$(zfs get -pH -o value name,type,mounted,mountpoint,readonly,canmount "$DATASET" | xargs echo -n | tr '\n' ' ')
if [ "$OUTPUT" != "$DATASET filesystem yes $MOUNTPOINT off on" ]; then
  echo >&2 "Error: Cannot ensure $DATASET is mounted correctly at $MOUNTPOINT, got $OUTPUT (name,type,mounted,mountpoint,readonly,canmount)"
  exit 1
fi

OUTPUT=$(mount | grep -c "on $MOUNTPOINT type zfs")
if [ "$OUTPUT" != "1" ]; then
  echo >&2 "Error: Multiple ($OUTPUT) active mounts at $MOUNTPOINT, are two datasets active at the same time?"
  exit 1
fi
