#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

SSH_SOURCE=${1:-}
DEST_DATASET=${2:-}

if [ -z "$SSH_SOURCE" ] || [ -z "$DEST_DATASET" ]; then
  echo >&2 "Usage: zfs_backup_onsite SSH_SOURCE DEST_DATASET"
  exit 1
fi

# --rollback discards any drift on the received target before the
# incremental, so a touched/remounted dest dataset self-heals instead of
# wedging the next pull. --zfs-compressed sends already-compressed records
# as-is, avoiding a decompress/recompress round-trip on both ends.
# --keep-source 16384 is a high ceiling: with --no-snapshot the source
# host's own local run owns source thinning, so this pull never prunes the
# source; --keep-target does the real thinning of the received copies.
# No --progress flag: zfs-autobackup auto-enables progress when stderr is a
# tty (operator run) and stays quiet under the timer -- passing --progress
# would force it on and spam the journal.
f_trace zfs-autobackup \
  --buffer 256M \
  --no-snapshot \
  --exclude-received \
  --clear-mountpoint \
  --clear-refreservation \
  --rollback \
  --keep-source 16384 \
  --keep-target 10,1d1w,1w1m,1m10y \
  --set-properties readonly=on \
  --set-properties mountpoint=none \
  --verbose \
  --ssh-config /root/.ssh/config \
  --ssh-source "zfs_autobackup@$SSH_SOURCE" \
  --zfs-compressed \
  bak "$DEST_DATASET"
