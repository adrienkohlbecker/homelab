#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euo pipefail
trap 'eval echo "\# $BASH_COMMAND"' DEBUG

SSH_SOURCE=${1:-}
DEST_DATASET=${2:-}

if [ "$SSH_SOURCE" = "" ] || [ "$DEST_DATASET" = "" ]; then
  echo "Usage: zfs_backup_onsite SSH_SOURCE DEST_DATASET" >&2
  exit 1
fi

zfs-autobackup \
  --buffer 256M \
  --no-snapshot \
  --exclude-received \
  --clear-mountpoint \
  --clear-refreservation \
  --keep-source 16384 \
  --keep-target 10,1d1w,1w1m,1m10y \
  --set-properties readonly=on \
  --set-properties mountpoint=none \
  --verbose \
  --ssh-config /root/.ssh/config \
  --ssh-source "zfs_autobackup@$SSH_SOURCE" \
  --compress=zstd-fast \
  bak "$DEST_DATASET"
