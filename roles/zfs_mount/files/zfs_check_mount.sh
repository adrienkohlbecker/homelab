#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euo pipefail

DATASET=${1:-}
MOUNTPOINT=${2:-}

if [ "$DATASET" = "" ] || [ "$MOUNTPOINT" = "" ]; then
  echo "Usage: zfs_check_mount DATASET MOUNTPOINT" >&2
  exit 1
fi

OUTPUT=$(zfs get -pH -o value name,type,mounted,mountpoint,readonly,canmount $DATASET | xargs echo -n | tr '\n' ' ')
if [ "$OUTPUT" != "$DATASET filesystem yes $MOUNTPOINT off on" ]; then
  echo "Cannot ensure $DATASET is mounted correctly at $MOUNTPOINT, got $OUTPUT" >&2
  exit 1
fi
