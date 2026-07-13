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

DESTPATH=${DATASET//\//_}

MOUNTPOINT=$(zfs get mountpoint -H -o value "$DATASET")

# Boot environments keep canmount=noauto so sibling BEs never race to mount at
# boot; all other datasets are canmount=on.
if [[ "$DATASET" == rpool/ROOT/* ]]; then
  expected_canmount=noauto
else
  expected_canmount=on
fi
# Validate the mount first, ahead of every skip path below (no-snapshot and the
# change-gate): a broken mount is a fault the offsite run must surface
# (zfs_check_mount fails -> the caller's f_rescue bumps f_failed -> the unit
# exits 1 for monitoring), and skipping ahead of it would let a degraded
# /mnt/services or /mnt/data be silently reported as an up-to-date skip. The
# check is sub-second, so paying it on skip nights costs nothing.
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

# Skip bunk's expensive tree walk when written@ confirms that nothing changed
# since the snapshot last synced successfully. The root-owned local marker is
# advanced only after rsync succeeds; it cannot be a dataset property because
# that write would defeat the next comparison, nor live on write-only bunk.
# Missing, thinned, or 30-day-old markers fall through to a full reconciliation.
# The root dataset contains the marker directory and therefore never skips, which
# is acceptable because the large /mnt and tank datasets are the costly walks.
MARKER_FILE="/var/lib/zfs_backup_offsite/$DESTPATH"

if [ -f "$MARKER_FILE" ] && [ -z "$(find "$MARKER_FILE" -mmin +43200 -print)" ]; then
  marker_snapshot=$(<"$MARKER_FILE")
  if written=$(zfs get -Hp -o value "written@$marker_snapshot" "$DATASET" 2>/dev/null) && [ "$written" = "0" ]; then
    echo "No change on $DATASET since @$marker_snapshot (written=0), skipping offsite sync"
    exit 0
  fi
fi

# rsync's running progress is helpful when an operator runs this by hand but is
# just \r-spam in the journal when the timer runs it, so only request it when
# stdout is a tty. (zfs-autobackup auto-detects this itself; rsync does not.)
rsync_progress=()
if [ -t 1 ]; then
  rsync_progress=(--info=progress2)
fi

# Both ends use rsync >= 3.2, so compression and checksums can negotiate their
# fastest common algorithms. Do not pin those choices: falling back to DSM's
# older rsync should degrade performance rather than break the backup. --fuzzy
# lets renamed files reuse an existing basis, and --partial-dir resumes them.
#
# Do not add --acls: bunk's SynoCli build has no ACL support. Mode, ownership,
# and device metadata still round-trip through xattrs and remote fake-super.
# The "*@*:*:*~" exclude drops ansible backup files (backup: true writes
# <file>.<pid>.<date>@<HH:MM:SS>~) because many contain prior secret values;
# --delete-excluded also purges copies already present on bunk.
#
# --timeout measures rsync-protocol inactivity, not tree-walk duration. The
# 1800-second bound tolerates bunk's longest legitimate traversal stalls while
# still failing a dead connection before the unit timeout. The caller pauses
# after failure so rrsync can release its per-host lock.
# The output format preserves dataset context on every update sent through
# journald and Fluent Bit.
f_trace rsync \
  --archive \
  --hard-links \
  --xattrs \
  --human-readable \
  --sparse \
  --delete \
  --delete-excluded \
  --timeout 1800 \
  --compress \
  --partial-dir .rsync-partial \
  --fuzzy \
  "${rsync_progress[@]}" \
  --devices \
  --specials \
  -M--fake-super \
  --numeric-ids \
  --stats \
  --out-format="dataset=$DATASET change=%i path=%n%L" \
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
  "${MOUNTPOINT%/}/.zfs/snapshot/$LAST_SNAPSHOT/" "ak@$OFFSITE_IP:$DESTPATH"

# Record the snapshot now mirrored on bunk so the change-gate above can skip
# unchanged nights, and stamp the marker's mtime for the 30-day reconciliation
# clock. Reached only on rsync success: functions.sh sets errexit, so a failed
# rsync aborts the script before this line, leaving the marker at the last good
# sync for the next run to retry from.
echo "$LAST_SNAPSHOT" >"$MARKER_FILE"

# The destination is relative on purpose. bunk forces this key through rrsync
# (command="rrsync -wo /volume1/Backup/<host>"), which chdirs into that per-host
# root and resolves the path against it -- so "$DESTPATH" lands in
# /volume1/Backup/<host>/. An absolute path would get the restricted root
# prepended (double-pathed) and rejected. See host_vars/bunk.yml.
