#!/bin/bash
# shellcheck disable=SC2029  # every remote command deliberately expands $TARGET_DATASET/$MOUNTPOINT locally
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

# Restore a replica tree onto a freshly rebuilt host -- the reverse of the
# nightly onsite pull. Runs on the replica holder as the operator, NOT root:
# the ssh hop to the target authenticates with the operator's own key/agent,
# while the local zfs send and every remote zfs command run under sudo.
#
# Sends the newest snapshot of REPLICA_DATASET recursively (-R carries child
# datasets and all snapshots), then resets on the target the properties the
# onsite pull had overridden on the replica: readonly=off, canmount=on,
# MOUNTPOINT on the root of the tree (children revert to path inheritance),
# and autobackup:bak back to a local source on every dataset the stream
# carried it on -- received-source tags are skipped by the `-s local` picker,
# which would silently drop the restored data from all future backups.
#
# Interactive by design: previews the stream and prompts before sending.
# --yes skips the prompt for scripted use (the test harness).
#
# Usage: zfs_backup_restore [--yes] TARGET_SSH REPLICA_DATASET TARGET_DATASET MOUNTPOINT
#   zfs_backup_restore ak@10.123.0.3 tank/pug/rpool/services rpool/services /mnt/services

ASSUME_YES=0
if [ "${1:-}" = --yes ]; then
	ASSUME_YES=1
	shift
fi

TARGET_SSH=${1:-}
REPLICA_DATASET=${2:-}
TARGET_DATASET=${3:-}
MOUNTPOINT=${4:-}

if [ -z "$TARGET_SSH" ] || [ -z "$REPLICA_DATASET" ] || [ -z "$TARGET_DATASET" ] || [ -z "$MOUNTPOINT" ]; then
	echo >&2 "Usage: zfs_backup_restore [--yes] TARGET_SSH REPLICA_DATASET TARGET_DATASET MOUNTPOINT"
	exit 1
fi

SNAP=$(zfs list -t snapshot -H -o name -s creation "$REPLICA_DATASET" | tail -1)
if [ -z "$SNAP" ]; then
	echo >&2 "ERROR: no snapshot found on $REPLICA_DATASET"
	exit 1
fi

# recv -F rolls back / clobbers the target, so prove the far end is the
# freshly-rebuilt host (no target dataset) and not some other machine a stale
# DNS answer pointed us at, before sending anything.
REMOTE_HOSTNAME=$(ssh "$TARGET_SSH" 'hostnamectl --static')
if ssh "$TARGET_SSH" "sudo zfs list -H -o name '$TARGET_DATASET'" >/dev/null 2>&1; then
	echo >&2 "ERROR: $TARGET_DATASET already exists on $REMOTE_HOSTNAME -- refusing to clobber; destroy it first if this is intentional"
	exit 1
fi

# Send-side preview: lists every snapshot with sizes (the oldest shown as
# `full`, proving a full restore) plus a total estimate. A receive-side
# `recv -n` dry-run is NOT usable here -- it never materialises the target,
# so it aborts on the first in-stream incremental.
f_trace sudo zfs send -Rnv "$SNAP"

echo
echo "Restoring $SNAP -> $REMOTE_HOSTNAME ($TARGET_SSH) as $TARGET_DATASET, mountpoint $MOUNTPOINT"
if ((!ASSUME_YES)); then
	read -r -p "Proceed? [y/N] " REPLY </dev/tty
	if [ "$REPLY" != y ]; then
		echo >&2 "Aborted"
		exit 1
	fi
fi

# mbuffer (unmuted) shows a live throughput + ETA meter on top of send -v's
# periodic progress lines. recv -u defers all mounting to the property reset
# below -- letting recv auto-mount at the stream-carried mountpoints could
# shadow (or refuse over) a live directory on the target.
sudo zfs send -Rv "$SNAP" | mbuffer -m 256M | ssh "$TARGET_SSH" "sudo zfs recv -uF '$TARGET_DATASET'"

ssh "$TARGET_SSH" "sudo bash -s -- '$TARGET_DATASET' '$MOUNTPOINT'" <<'REMOTE'
set -euo pipefail
target=$1
mountpoint=$2

# The onsite pull received the replica with stream-wide overrides --
# readonly=on, mountpoint=none, canmount=noauto -- so the original values of
# those three properties are unrecoverable from the stream. Reset the whole
# tree to sane defaults instead: writable, mountable, children on path
# inheritance under the root's new mountpoint. Converge re-asserts the
# role-declared values afterwards.
#
# The root is mounted before any child is touched: a child's property reset
# can auto-mount it (canmount/mountpoint changes mount an unmounted dataset),
# and a child mounting first would leave a directory inside the root's
# still-unmounted mountpoint, failing the root mount with "not empty".
zfs set readonly=off canmount=on mountpoint="$mountpoint" "$target"
if [ "$(zfs get -H -o value mounted "$target")" = no ]; then
  zfs mount "$target"
fi
for ds in $(zfs list -r -H -o name "$target" | tail -n +2); do
  zfs set readonly=off canmount=on "$ds"
  zfs inherit mountpoint "$ds"
  if [ "$(zfs get -H -o value mounted "$ds")" = no ]; then
    zfs mount "$ds"
  fi
done

# Restore the local backup tag on every dataset the stream carried it on.
# Converge self-heals received-source tags too; doing it here means the tree
# is correctly tagged even before the first converge.
while IFS=$'\t' read -r ds value; do
  if [ "$value" = true ]; then
    zfs set autobackup:bak=true "$ds"
  fi
done < <(zfs get -t filesystem -r -H -o name,value autobackup:bak "$target")

echo
zfs list -r -o name,mountpoint,mounted,readonly "$target"
zfs get -t filesystem -r -o name,value,source autobackup:bak "$target"
REMOTE

echo "Restore complete. Converge the host next -- it re-asserts the remaining dataset properties."
