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

# rsync's running progress is helpful when an operator runs this by hand but is
# just \r-spam in the journal when the timer runs it, so only request it when
# stdout is a tty. (zfs-autobackup auto-detects this itself; rsync does not.)
rsync_progress=()
if [ -t 1 ]; then
  rsync_progress=(--info=progress2)
fi

# Both ends now speak rsync >= 3.2 (the source hosts, and bunk via rrsync's
# RSYNC override at SynoCli 3.4.1), unlocking the negotiated fast paths: plain
# --compress auto-picks zstd, the per-block strong checksum auto-negotiates xxh3
# (faster and stronger than the MD4 floor 3.1.2 forced), and --partial-dir now
# resumes a cut-off transfer in-place instead of restarting the file. We do NOT
# pin --compress-choice/--checksum-choice on purpose: if a DSM update ever resets
# rrsync to stock 3.1.2, the backup should degrade gracefully rather than
# hard-fail on an unsupported algorithm. --fuzzy lets a renamed or rotated file
# (log.1, dump-<date>.sql) delta against a similar basis already in the dest
# instead of resending it whole -- nearly free, since same-named files match
# first and never trigger the fuzzy search. Load-bearing dependency: bunk's
# rrsync must point at 3.4.1 (roles/zfs_autobackup/files/rrsync).
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
  --fuzzy \
  "${rsync_progress[@]}" \
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
