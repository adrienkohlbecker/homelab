#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

# Restore the final services snapshot from a pull replica onto a rebuilt host.
# Runs on the replica holder as the operator: local zfs send and remote ZFS
# commands use sudo, while SSH authenticates with the operator's key or agent.
#
# The replica holder has local receive overrides (readonly=on, mountpoint=none,
# canmount=noauto). `zfs send -b` excludes those overrides and sends the original
# received properties. The target root is remapped to the requested mountpoint,
# and backup tags are made local so the next `-s local` snapshot selection
# includes them.
#
# Interrupted receives are resumable. A partial target is accepted only when it
# carries a receive_resume_token; every other existing target fails closed.
#
# Usage: zfs_backup_restore TARGET_SSH REPLICA_DATASET SNAPSHOT_SUFFIX TARGET_DATASET MOUNTPOINT
#   zfs_backup_restore ak@10.123.0.3 tank/pug/rpool/services bak-20260710235959 rpool/services /mnt/services

usage() {
  echo >&2 "Usage: zfs_backup_restore TARGET_SSH REPLICA_DATASET SNAPSHOT_SUFFIX TARGET_DATASET MOUNTPOINT"
  exit 2
}

if (($# != 5)); then
  usage
fi

TARGET_SSH=$1
REPLICA_DATASET=$2
SNAPSHOT_SUFFIX=$3
TARGET_DATASET=$4
MOUNTPOINT=$5

# These values never become shell source on the far end, but reject option-like
# destinations and malformed snapshot names before invoking privileged tools.
if [[ $TARGET_SSH == -* || ! $TARGET_SSH =~ ^[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9_.:-]+$ ]]; then
  echo >&2 "ERROR: invalid SSH destination: $TARGET_SSH"
  exit 2
fi
if [[ ! $REPLICA_DATASET =~ ^[A-Za-z0-9][A-Za-z0-9_.:/-]*$ ]]; then
  echo >&2 "ERROR: unsupported replica dataset name: $REPLICA_DATASET"
  exit 2
fi
if [[ ! $TARGET_DATASET =~ ^[A-Za-z0-9][A-Za-z0-9_.:/-]*$ ]]; then
  echo >&2 "ERROR: unsupported target dataset name: $TARGET_DATASET"
  exit 2
fi
if [[ ! $MOUNTPOINT =~ ^/[A-Za-z0-9_.:/-]+$ || $MOUNTPOINT == */../* || $MOUNTPOINT == */.. ]]; then
  echo >&2 "ERROR: unsupported target mountpoint: $MOUNTPOINT"
  exit 2
fi
if [[ ! $SNAPSHOT_SUFFIX =~ ^bak-[0-9]{14}$ ]]; then
  echo >&2 "ERROR: snapshot suffix must match bak-YYYYMMDDhhmmss"
  exit 2
fi

if ! replica_tree=$(zfs list -r -H -o name "$REPLICA_DATASET"); then
  echo >&2 "ERROR: replica dataset does not exist: $REPLICA_DATASET"
  exit 1
fi

SNAP=${REPLICA_DATASET}@${SNAPSHOT_SUFFIX}
while IFS= read -r ds; do
  if [[ ! $ds =~ ^[A-Za-z0-9_.:/-]+$ ]]; then
    echo >&2 "ERROR: unsupported dataset name: $ds"
    exit 1
  fi
  if ! zfs list -t snapshot -H -o name "${ds}@${SNAPSHOT_SUFFIX}" >/dev/null; then
    echo >&2 "ERROR: recursive restore snapshot missing: ${ds}@${SNAPSHOT_SUFFIX}"
    exit 1
  fi
done <<<"$replica_tree"

remote_state() {
  # TARGET_DATASET is constrained to shell-safe ZFS name characters above.
  # shellcheck disable=SC2029
  ssh "$TARGET_SSH" "sudo bash -s -- '$TARGET_DATASET'" <<'REMOTE'
set -euo pipefail
target=$1

if ! datasets=$(zfs list -H -o name); then
  echo >&2 "ERROR: cannot enumerate target datasets"
  exit 20
fi
if ! grep -Fxq "$target" <<<"$datasets"; then
  echo absent
  exit 0
fi

token_rows=$(zfs get -r -H -o name,value receive_resume_token "$target")
resume_count=0
while IFS=$'\t' read -r dataset token; do
  if [ "$token" != - ]; then
    resume_dataset=$dataset
    resume_token=$token
    resume_count=$((resume_count + 1))
  fi
done <<<"$token_rows"

if [ "$resume_count" -eq 0 ]; then
  echo exists
elif [ "$resume_count" -eq 1 ]; then
  printf 'resume\t%s\t%s\n' "$resume_dataset" "$resume_token"
else
  echo >&2 "ERROR: multiple interrupted receive tokens exist below $target"
  exit 21
fi
REMOTE
}

remote_dataset_exists() {
  local target_dataset=$1
  ssh -n "$TARGET_SSH" "sudo zfs list -H -o name '$target_dataset'" >/dev/null 2>&1
}

# A multiplexed session inherits the master connection's cipher and compression.
# Use a dedicated connection for the already-compressed ZFS stream, preferring
# hardware-accelerated AES-GCM while retaining the remaining configured ciphers
# as fallbacks.
SSH_BULK_OPTIONS=(
  -o ControlPath=none
  -o Compression=no
  -o 'Ciphers=^aes128-gcm@openssh.com'
)

receive_tree() {
  local source_dataset=$1 target_dataset=$2
  # target_dataset is derived from the validated replica tree.
  # shellcheck disable=SC2029
  sudo zfs send -Rbcv "${source_dataset}@${SNAPSHOT_SUFFIX}" |
    mbuffer -m 256M |
    ssh "${SSH_BULK_OPTIONS[@]}" "$TARGET_SSH" "sudo zfs recv -su '$target_dataset'"
}

REMOTE_HOSTNAME=$(ssh -n "$TARGET_SSH" 'hostnamectl --static')
if ! initial_state=$(remote_state); then
  echo >&2 "ERROR: failed to inspect $TARGET_DATASET on $REMOTE_HOSTNAME"
  exit 1
fi

case "$initial_state" in
absent)
  echo "Starting a new restore"
  f_trace sudo zfs send -Rnbcv "$SNAP"
  ;;
exists)
  echo >&2 "ERROR: $TARGET_DATASET already exists on $REMOTE_HOSTNAME without a resume token -- refusing to overwrite it"
  exit 1
  ;;
resume$'\t'*)
  resume_fields=${initial_state#*$'\t'}
  resume_dataset=${resume_fields%%$'\t'*}
  resume_token=${resume_fields#*$'\t'}
  if [[ ! $resume_dataset =~ ^[A-Za-z0-9][A-Za-z0-9_.:/-]*$ ]] ||
    { [ "$resume_dataset" != "$TARGET_DATASET" ] && [[ $resume_dataset != "$TARGET_DATASET/"* ]]; }; then
    echo >&2 "ERROR: invalid resume dataset returned by $REMOTE_HOSTNAME"
    exit 1
  fi
  if [[ ! $resume_token =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo >&2 "ERROR: invalid receive resume token returned by $REMOTE_HOSTNAME"
    exit 1
  fi
  echo "Resuming an interrupted restore into $resume_dataset"
  f_trace sudo zfs send -nP -t "$resume_token"
  ;;
*)
  echo >&2 "ERROR: unexpected target state from $REMOTE_HOSTNAME: $initial_state"
  exit 1
  ;;
esac

echo
echo "Restoring $SNAP -> $REMOTE_HOSTNAME ($TARGET_SSH) as $TARGET_DATASET"
if ! read -r -p "Proceed? [y/N] " REPLY || [ "$REPLY" != y ]; then
  echo >&2 "Aborted"
  exit 1
fi

# The preview and prompt may take time. Refuse if anything changed remotely
# before the receive; recv without -F is the final fail-closed race guard.
if ! current_state=$(remote_state); then
  echo >&2 "ERROR: failed to re-check $TARGET_DATASET on $REMOTE_HOSTNAME"
  exit 1
fi
if [ "$current_state" != "$initial_state" ]; then
  echo >&2 "ERROR: $TARGET_DATASET changed after preflight -- refusing to continue"
  exit 1
fi

if [ "$initial_state" = absent ]; then
  receive_tree "$REPLICA_DATASET" "$TARGET_DATASET"
else
  # resume_dataset is constrained to the requested target tree above.
  # shellcheck disable=SC2029
  sudo zfs send -t "$resume_token" |
    mbuffer -m 256M |
    ssh "${SSH_BULK_OPTIONS[@]}" "$TARGET_SSH" "sudo zfs recv -su '$resume_dataset'"

  # A resume token covers one filesystem stream, not later members of a
  # recursive replication package. Send only subtrees that did not arrive.
  while IFS= read -r ds; do
    target_ds=${TARGET_DATASET}${ds#"$REPLICA_DATASET"}
    if ! remote_dataset_exists "$target_ds"; then
      receive_tree "$ds" "$target_ds"
    fi
  done <<<"$replica_tree"
fi

# Do not make an incomplete tree writable if any recursive stream was omitted.
while IFS= read -r ds; do
  target_ds=${TARGET_DATASET}${ds#"$REPLICA_DATASET"}
  if ! remote_dataset_exists "$target_ds" ||
    ! ssh -n "$TARGET_SSH" "sudo zfs list -t snapshot -H -o name '${target_ds}@${SNAPSHOT_SUFFIX}'" >/dev/null 2>&1; then
    echo >&2 "ERROR: restore incomplete: ${target_ds}@${SNAPSHOT_SUFFIX} is missing"
    exit 1
  fi
done <<<"$replica_tree"

# Both arguments are constrained to shell-safe characters above.
# shellcheck disable=SC2029
ssh "$TARGET_SSH" "sudo bash -s -- '$TARGET_DATASET' '$MOUNTPOINT'" <<'REMOTE'
set -euo pipefail
target=$1
mountpoint=$2

# The root location is selected by the operator. Child properties come from
# the source via `zfs send -b`, including deliberate non-default values.
zfs set readonly=off canmount=on mountpoint="$mountpoint" "$target"

datasets=$(zfs list -r -H -o name "$target")
while IFS= read -r ds; do
  ds_mountpoint=$(zfs get -H -o value mountpoint "$ds")
  ds_canmount=$(zfs get -H -o value canmount "$ds")
  ds_mounted=$(zfs get -H -o value mounted "$ds")
  if [ "$ds_mountpoint" != none ] && [ "$ds_mountpoint" != legacy ] && [ "$ds_canmount" != off ] && [ "$ds_mounted" = no ]; then
    zfs mount "$ds"
  fi
done <<<"$datasets"

# Received-source tags are skipped by the local snapshot picker. Localize every
# true tag before converge so the restored tree immediately rejoins backups.
backup_rows=$(zfs get -t filesystem -r -H -o name,value autobackup:bak "$target")
while IFS=$'\t' read -r ds value; do
  if [ "$value" = true ]; then
    zfs set autobackup:bak=true "$ds"
  fi
done <<<"$backup_rows"

echo
zfs list -r -o name,mountpoint,mounted,readonly "$target"
zfs get -t filesystem -r -o name,value,source autobackup:bak "$target"
REMOTE

echo "Restore complete. Converge the host next -- it re-asserts the remaining dataset properties."
