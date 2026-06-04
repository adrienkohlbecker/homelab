#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

OFFSITE_IP=${1:-}
DATASET=${2:-}

if [ -z "$OFFSITE_IP" ] || [ -z "$DATASET" ]; then
  echo >&2 "Usage: zfs_backup_offsite OFFSITE_IP DATASET"
  exit 1
fi

MOUNTPOINT=$(zfs get mountpoint -H -o value "$DATASET")
DESTPATH=${DATASET//\//_}

zfs_check_mount "$DATASET" "$MOUNTPOINT"

# awk collapses the grep|tail|cut pipeline: snapshots arrive sorted by
# creation (ascending), so the last @bak- line is the newest. A bare
# grep would exit 1 (tripping pipefail) on a dataset with no @bak-
# snapshot yet -- awk just yields an empty string.
LAST_SNAPSHOT=$(zfs list -t snapshot -o name -s creation -r "$DATASET" | awk -F@ '/@bak-/ {s=$2} END {print s}')

# A freshly-tagged or just-thinned dataset has no @bak- snapshot. Skip it
# cleanly rather than rsyncing the empty path ".zfs/snapshot//" (the whole
# snapshot root) with --delete, which would mirror every snapshot offsite.
if [ -z "$LAST_SNAPSHOT" ]; then
  echo >&2 "No @bak- snapshot for $DATASET, skipping offsite sync"
  exit 0
fi

# zstd on the wire: both ends are now rsync >= 3.2 (the source hosts, and bunk
# via rrsync's RSYNC override at SynoCli 3.4.1), so plain --compress
# auto-negotiates zstd. DSM's stock rsync 3.1.2 only spoke deflate and broke
# negotiation -- which is why this was bare before. Load-bearing dependency:
# bunk's rrsync must point at 3.4.1 (roles/zfs_autobackup/files/rrsync).
f_trace rsync \
  --archive \
  --hard-links \
  --acls \
  --xattrs \
  --human-readable \
  --sparse \
  --delete \
  --delete-excluded \
  --timeout 60 \
  --compress \
  --partial-dir .rsync-partial \
  --info progress2 \
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
  --exclude /var/lib/containers \
  --exclude /home/ak/.local/share/containers \
  --exclude /var/crash \
  "${MOUNTPOINT%"/"}/.zfs/snapshot/$LAST_SNAPSHOT/" "ak@$OFFSITE_IP:$DESTPATH"

# The destination is relative on purpose. bunk forces this key through rrsync
# (command="rrsync -wo /volume1/Backup/<host>"), which chdirs into that per-host
# root and resolves the path against it -- so "$DESTPATH" lands in
# /volume1/Backup/<host>/. An absolute path would get the restricted root
# prepended (double-pathed) and rejected. See host_vars/bunk.yml.
