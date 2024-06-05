#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

OFFSITE_IP=${1:-}
DATASET=${2:-}

if [ -z "$OFFSITE_IP" ] || [ -z "$DATASET" ]; then
  f_fail "Usage: zfs_backup_offsite OFFSITE_IP DATASET"
fi

MOUNTPOINT=$(zfs get mountpoint -H -o value "$DATASET")
DESTPATH=${DATASET//\//_}

zfs_check_mount "$DATASET" "$MOUNTPOINT"

LAST_SNAPSHOT=$(zfs list -t snapshot -o name -s creation -r "$DATASET" | grep "@bak-" | tail -1 | cut -d@ -f2)

f_trace rsync \
  --archive \
  --hard-links \
  --acls \
  --xattrs \
  --human-readable \
  --sparse \
  --delete \
  --delete-excluded \
  --compress \
  --timeout 60 \
  --devices \
  --specials \
  -M--fake-super \
  --numeric-ids \
  --stats \
  --log-file="/var/log/zfs_autobackup/$DESTPATH.log" \
  --one-file-system \
  --exclude .DS_Store \
  --exclude "._*" \
  --exclude .DocumentRevisions-V100 \
  --exclude .Trashes \
  --exclude .TemporaryItems \
  "${MOUNTPOINT%"/"}/.zfs/snapshot/$LAST_SNAPSHOT/" "ak@$OFFSITE_IP:/volume1/Backup/$(hostname -s)/$DESTPATH"
