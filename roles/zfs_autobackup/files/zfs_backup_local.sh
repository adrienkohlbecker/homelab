#!/bin/bash
set -euo pipefail
((EUID == 0)) || {
  echo >&2 "Error: I require root"
  exit 1
}

# --no-resume leaves zfs_autosnapshot.target stopped after the snapshots. The
# rebuild runbooks use it to quiesce: the default post-snapshot restart would
# bring the state writers back up between the final snapshot and the wipe.
post_snapshot_cmd='systemctl start zfs_autosnapshot.target'
case "${1:-}" in
'') ;;
--no-resume) post_snapshot_cmd=true ;;
*)
  echo >&2 "Usage: zfs_backup_local [--no-resume]"
  exit 2
  ;;
esac

# --no-send takes only local snapshots, so --keep-source is the live
# retention schedule; --keep-target 16384 is an inert ceiling (nothing is
# sent to a target here) kept only to satisfy the thinner's defaults.
zfs_autobackup_cmd=(zfs-autobackup
  --no-send
  --keep-source "10,1d1w,1w1m,1m10y"
  --keep-target 16384
  --allow-empty
  --exclude-received
  --post-snapshot-cmd "$post_snapshot_cmd"
  --pre-snapshot-cmd 'systemctl stop zfs_autosnapshot.target'
  --verbose
  bak)
printf '$%s\n' "$(printf ' %q' "${zfs_autobackup_cmd[@]}")"
"${zfs_autobackup_cmd[@]}"
