#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euo pipefail
trap 'eval echo "\# $BASH_COMMAND"' DEBUG

DATASET=${1:-}
MOUNTPOINT=${2:-}

if [ "$DATASET" = "" ] || [ "$MOUNTPOINT" = "" ]; then
  echo >&2 "Usage: zfs_check_mount DATASET MOUNTPOINT"
  exit 1
fi

OUTPUT=$(zfs get -pH -o value name,type,mounted,mountpoint,readonly,canmount $DATASET | xargs echo -n | tr '\n' ' ')
if [ "$OUTPUT" != "$DATASET filesystem yes $MOUNTPOINT off on" ]; then
  echo >&2 "Cannot ensure $DATASET is mounted correctly at $MOUNTPOINT, got $OUTPUT (name,type,mounted,mountpoint,readonly,canmount)"
  exit 1
fi

OUTPUT=$(mount | grep "on /mnt/services type zfs" | wc -l)
if [ "$OUTPUT" != "1" ]; then
  echo >&2 "Multiple ($OUTPUT) active mounts at $MOUNTPOINT, are two datasets active at the same time?"
  exit 1
fi
