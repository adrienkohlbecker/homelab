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

# Change-gate: skip the nightly rsync when nothing has changed since the last
# successful offsite sync. rsync's cost here is dominated by walking the whole
# dataset tree on bunk's slow disks, even on the many nights when -- for large,
# seldom-written datasets (tank/data, the dormant parent-NAS mirrors) -- there is
# nothing to send. `written@<snap>` is the cumulative bytes written to the
# dataset since that snapshot: an O(1) property read (no tree walk), and sound in
# the skip direction -- any create/modify/delete writes blocks, so 0 means
# nothing changed and bunk's copy is already current. Runs after the mount check
# above, so a skip still implies a validated mount.
#
# The marker records the snapshot last confirmed on bunk. It lives OUTSIDE the
# backed-up dataset on purpose: a marker stored as a dataset property would itself
# write metadata, so the next night's written@ could never read 0. It is also
# local, not on bunk: bunk's rrsync is write-only (-wo), so a source cannot read a
# marker back from it. It is advanced only after a successful rsync (see below).
# One dataset is unavoidably self-referential: the root dataset
# (rpool/ROOT/<release>, mounted at /) holds both this marker dir and the rsync
# --log-file under /var, so its own written@ is never 0 and root never skips.
# That is fine -- root is small and was never the slow-walk target; the gate pays
# off on the large /mnt and tank/* data datasets, which carry neither path.
#
# Fall through to a full rsync -- (re)establishing the baseline -- when there is
# no marker yet (first run); the marker snapshot was thinned away so written@
# errors; or the last successful sync was over 30 days ago (the marker file's
# mtime). That last case is a monthly reconciliation: a full rsync re-verifies
# the whole tree and heals any silent bunk-side drift even on datasets that never
# change (where no change-night would otherwise ever re-sync).
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
# --timeout is an rsync-protocol I/O-inactivity timer (the rsync default is
# 0/off): a dead connection to the NAS should still fail eventually rather than
# hang until the unit 23h ceiling. It does NOT bound the walk -- incremental
# recursion (default in rsync 3.x, undisabled by our flags) streams the file
# list as it scans, so the socket only goes quiet *mid-transfer*, while the slow
# Synology does delete-reconciliation and directory traversal with no rsync
# traffic to send. ssh/TCP keepalives can't reset it (they aren't rsync bytes),
# so the value is the only knob. The quiet stall scales with the dataset's tree:
# 60s was too tight, then 600s, and at tank/data's 2.64TB even 600s trips a
# code-30 io-timeout. That abort is not just one lost dataset: the offsite loop
# moves to the next dataset immediately (f_rescue), but bunk rrsync still holds
# the per-host fcntl flock for the ~1-2s it takes to notice the dropped
# connection, so the next dataset dies with "Another instance of rrsync is
# already accessing this directory" (code 12). 1800s tolerates the legitimate
# NAS stalls on the largest dataset and removes that cascade.
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
