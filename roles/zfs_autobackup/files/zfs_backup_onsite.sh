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

# Reads snapshot names and prints each dataset with its newest @bak- snapshot,
# tab-separated; bak-%Y%m%d%H%M%S names sort chronologically.
newest_bak_per_dataset() {
  awk -F @ '$2 ~ /^bak-/ && $2 > newest[$1] { newest[$1] = $2 } END { for (ds in newest) print ds "\t" newest[ds] }'
}

# Prints the source datasets this pull would still ship stale: every dataset
# tagged autobackup:bak=true at source local (the selection the nightly local
# leg snapshots) whose newest @bak- is not newer than its replica's newest at
# DEST_DATASET/<dataset>. A dataset not yet replicated is never behind.
# Fails when the source cannot be read.
datasets_behind() {
  local tagged sources
  tagged=$(ssh -n -F /etc/zfs_autobackup_ssh_config "zfs_autobackup@$SSH_SOURCE" \
    zfs get -H -s local -t filesystem,volume -o name,value autobackup:bak |
    awk -F '\t' '$2 == "true" { print $1 }') || return 1
  sources=$(ssh -n -F /etc/zfs_autobackup_ssh_config "zfs_autobackup@$SSH_SOURCE" \
    zfs list -H -t snapshot -o name | newest_bak_per_dataset) || return 1
  awk -F '\t' -v dest="$DEST_DATASET/" '
    FILENAME == ARGV[1] { if (index($1, dest) == 1) replica[substr($1, length(dest) + 1)] = $2; next }
    FILENAME == ARGV[2] { source[$1] = $2; next }
    NF && $1 in replica && !(source[$1] > replica[$1]) { print $1 }
  ' <(printf '%s\n' "$replicas") <(printf '%s\n' "$sources") <(printf '%s\n' "$tagged")
}

# Every host's nightly run fires at its own stable offset within five
# minutes, so a peer can still be taking tonight's snapshot when this pull
# starts, and the pull would ship last night's. The nightly wrapper waits,
# bounded, until every dataset the source tags holds a snapshot newer than its
# replica. Comparing per dataset keeps an out-of-band snapshot of one dataset
# (a zfs_dataset retirement, a manual snapshot) from releasing the wait for the
# others. On timeout it pulls what exists and zfs_backup_freshness reports a
# source that stays behind.
if ((wait_seconds > 0)); then
  # A first-ever pull has no replicas yet.
  replicas=$(zfs list -H -t snapshot -o name -r "$DEST_DATASET" 2>/dev/null | newest_bak_per_dataset || true)
  deadline=$((SECONDS + wait_seconds))
  while :; do
    # A transient SSH failure reads as still behind, so the loop keeps waiting
    # rather than aborting or pulling early.
    behind=$(datasets_behind) || behind="(source unreadable)"
    [ -z "$behind" ] && break
    if ((SECONDS >= deadline)); then
      echo >&2 "Warning: $SSH_SOURCE has no snapshot newer than its replica after ${wait_seconds}s for: ${behind//$'\n'/ }; pulling what exists"
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
