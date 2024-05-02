#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

SSH_SOURCE=${1:-}
DEST_DATASET=${2:-}

if [ -n "$SSH_SOURCE" ] || [ -n "$DEST_DATASET" ]; then
  f_fail "Usage: zfs_backup_onsite SSH_SOURCE DEST_DATASET"
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
