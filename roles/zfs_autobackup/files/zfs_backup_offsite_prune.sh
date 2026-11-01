#!/bin/bash
set -euo pipefail
((EUID == 0)) || {
  echo >&2 "Error: I require root"
  exit 1
}

OFFSITE_IP=${1:-}

if [ -z "$OFFSITE_IP" ]; then
  echo >&2 "Usage: zfs_backup_offsite_prune OFFSITE_IP"
  exit 1
fi

# zfs_backup_offsite's --delete works inside one dataset's tree, so the tree of
# a dataset this host no longer selects would otherwise stay on bunk forever.
# Keep every dataset still selected, whatever the property's source: a retired
# dataset keeps the copy from before its retirement until it is destroyed or
# deselected, and a dataset restored with a received property still owns its
# tree. Assigned first so a zfs failure aborts here, ahead of any deletion.
selected=$(zfs get -H -t filesystem -o name,value autobackup:bak | awk -F '\t' '$2 == "true" {print $1}')

# With nothing protected, syncing the empty source would wipe this host's whole
# offsite root, so an empty selection is a fault rather than a host to clear.
if [ -z "$selected" ]; then
  echo >&2 "Error: no dataset selected by autobackup:bak, refusing to prune offsite"
  exit 1
fi

# DSM's indexer creates @eaDir at any level; it is not a backup tree.
filters=(--filter 'P /@eaDir/')
while read -r dataset; do
  filters+=(--filter "P /${dataset//\//_}/")
done <<<"$selected"

empty=$(mktemp -d)
trap 'rmdir "$empty"' EXIT

# An empty source mirrored onto the forced-command root deletes each unprotected
# top-level entry and nothing else. No --archive: the root's own metadata stays.
rsync \
  --rsh "ssh -F /etc/zfs_autobackup_ssh_config" \
  --recursive \
  --delete \
  --timeout 1800 \
  --stats \
  --out-format="prune change=%i path=%n%L" \
  "${filters[@]}" \
  "$empty/" "ak@$OFFSITE_IP:."
