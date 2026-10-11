#!/bin/bash
set -euo pipefail
((EUID == 0)) || {
  echo >&2 "Error: I require root"
  exit 1
}

usage="Usage: zfs_backup_onsite [--wait-for-source SECONDS] SSH_SOURCE DEST_DATASET"
wait_seconds=0
if [ "${1:-}" = --wait-for-source ]; then
  wait_seconds=${2:-}
  [[ $wait_seconds =~ ^[0-9]+$ ]] || {
    echo >&2 "$usage"
    exit 1
  }
  shift 2
fi
SSH_SOURCE=${1:-}
DEST_DATASET=${2:-}

if [ -z "$SSH_SOURCE" ] || [ -z "$DEST_DATASET" ]; then
  echo >&2 "$usage"
  exit 1
fi

# Newest @bak- snapshot name; bak-%Y%m%d%H%M%S names sort chronologically.
newest_bak() {
  awk -F @ '$2 ~ /^bak-/ && $2 > s { s = $2 } END { print s }'
}

# Every host's nightly run fires at its own stable offset within five
# minutes, so a peer can still be taking tonight's snapshot when this pull
# starts, and the pull would ship last night's. The nightly wrapper waits,
# bounded, for the source to hold a snapshot newer than this host's newest
# replica; on timeout it pulls what exists and zfs_backup_freshness reports a
# source that stays behind.
if ((wait_seconds > 0)); then
  # A first-ever pull has no replica yet, and a transient SSH failure reads as
  # nothing new; either way the loop keeps waiting rather than aborting.
  replica=$(zfs list -H -t snapshot -o name -r "$DEST_DATASET" 2>/dev/null | newest_bak || true)
  deadline=$((SECONDS + wait_seconds))
  while :; do
    source_newest=$(ssh -F /etc/zfs_autobackup_ssh_config "zfs_autobackup@$SSH_SOURCE" \
      zfs list -H -t snapshot -o name | newest_bak || true)
    [[ $source_newest > $replica ]] && break
    if ((SECONDS >= deadline)); then
      echo >&2 "Warning: $SSH_SOURCE has no snapshot newer than ${replica:-none} after ${wait_seconds}s; pulling what exists"
      break
    fi
    sleep 15
  done
fi

# --rollback discards any drift on the received target before the
# incremental, so a touched/remounted dest dataset self-heals instead of
# wedging the next pull. --zfs-compressed sends already-compressed records
# as-is, avoiding a decompress/recompress round-trip on both ends.
# --keep-source 16384 is a high ceiling: with --no-snapshot the source
# host's own local run owns source thinning, so this pull never prunes the
# source; --keep-target does the real thinning of the received copies.
# --set-properties takes one comma-separated list; a repeated flag replaces
# the earlier value instead of adding to it.
# No --progress flag: zfs-autobackup auto-enables progress when stderr is a
# tty (operator run) and stays quiet under the timer -- passing --progress
# would force it on and spam the journal.
zfs_autobackup_cmd=(zfs-autobackup
  --buffer 256M
  --no-snapshot
  --exclude-received
  --clear-mountpoint
  --clear-refreservation
  --rollback
  --keep-source 16384
  --keep-target "10,1d1w,1w1m,1m10y"
  --set-properties "readonly=on,mountpoint=none"
  --verbose
  --ssh-config /etc/zfs_autobackup_ssh_config
  --ssh-source "zfs_autobackup@$SSH_SOURCE"
  --zfs-compressed
  bak "$DEST_DATASET")
printf '$%s\n' "$(printf ' %q' "${zfs_autobackup_cmd[@]}")"
"${zfs_autobackup_cmd[@]}"
