#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euo pipefail
trap 'eval echo "\# $BASH_COMMAND"' DEBUG

OFFSITE_IP=${1:-}
DATASET=${2:-}

if [ "$OFFSITE_IP" = "" ] || [ "$DATASET" = "" ]; then
  echo "Usage: zfs_backup_offsite OFFSITE_IP DATASET" >&2
  exit 1
fi

MOUNTPOINT=$(zfs get mountpoint -H -o value "$DATASET")
DESTPATH=${DATASET//\//_}

zfs_check_mount "$DATASET" "$MOUNTPOINT"

LAST_SNAPSHOT=$(zfs list -t snapshot -o name -s creation -r "$DATASET" | grep "@bak-" | tail -1 | cut -d@ -f2)

rsync \
  --archive \
  --human-readable \
  --delete \
  --delete-excluded \
  --compress \
  --timeout 60 \
  --log-file="/var/log/zfs_autobackup/$DESTPATH.log" \
  --one-file-system \
  --exclude .DS_Store \
  --exclude "._*" \
  --exclude .DocumentRevisions-V100 \
  --exclude .Trashes \
  --exclude .TemporaryItems \
  "${MOUNTPOINT%"/"}/.zfs/snapshot/$LAST_SNAPSHOT/" "ak@$OFFSITE_IP:/volume1/Backup/$(hostname -s)/$DESTPATH"
