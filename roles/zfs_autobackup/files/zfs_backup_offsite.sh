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

# Boot environments keep canmount=noauto so sibling BEs never race to mount at
# boot; all other datasets are canmount=on.
if [[ "$DATASET" == rpool/ROOT/* ]]; then
  expected_canmount=noauto
else
  expected_canmount=on
fi
zfs_check_mount "$DATASET" "$MOUNTPOINT" "$expected_canmount"

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
# No --acls: bunk's SynoCli rsync (3.4.1, the build rrsync forces) is compiled
# "no ACLs" (`rsync --version` Capabilities), so requesting them makes the server
# bail with "ACLs are not supported on this server" / exit 1 -- which silently
# failed every offsite push. The datasets carry acltype=posix, so this drops
# supplementary POSIX ACLs from the DR copy; the base mode bits + ownership still
# round-trip via --xattrs + -M--fake-super (the SynoCli build *does* have xattrs).
# Stock DSM rsync has ACLs but is 3.1.2 -- too old to negotiate the protocol.
# The "*@*:*:*~" exclude drops ansible backup files (backup: true writes
# <file>.<pid>.<date>@<HH:MM:SS>~) from the DR copy: many render vaulted secrets,
# so shipping every rotation's previous value offsite is a plaintext-credential
# spill on a NAS we do not want to host secret history on. Paired with
# --delete-excluded, this also purges already-shipped backups from bunk. The glob
# keys on the literal @HH:MM:SS~ suffix so it matches ansible backups but not
# editor ~ files.
# --timeout is 600s (the rsync default is 0/off): a dead connection to the NAS
# should still fail eventually rather than hang until the unit 23h ceiling. 60s
# was too tight -- a large dataset (tank/data) routinely pauses >60s mid-transfer
# while the slow Synology checksums/deletes, tripping a code-30 io-timeout. That
# abort is not just one lost dataset: the offsite loop moves to the next dataset
# immediately (f_rescue), but bunk rrsync still holds the per-host fcntl flock
# for the ~1-2s it takes to notice the dropped connection, so the next dataset
# dies with "Another instance of rrsync is already accessing this directory"
# (code 12). 600s tolerates the legitimate NAS stalls and removes that cascade.
f_trace rsync \
  --archive \
  --hard-links \
  --xattrs \
  --human-readable \
  --sparse \
  --delete \
  --delete-excluded \
  --timeout 600 \
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
  --exclude "*@*:*:*~" \
  "${MOUNTPOINT%"/"}/.zfs/snapshot/$LAST_SNAPSHOT/" "ak@$OFFSITE_IP:$DESTPATH"

# The destination is relative on purpose. bunk forces this key through rrsync
# (command="rrsync -wo /volume1/Backup/<host>"), which chdirs into that per-host
# root and resolves the path against it -- so "$DESTPATH" lands in
# /volume1/Backup/<host>/. An absolute path would get the restricted root
# prepended (double-pathed) and rejected. See host_vars/bunk.yml.
