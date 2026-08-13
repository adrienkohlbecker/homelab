#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

# Restore an explicit snapshot range from a pull replica onto a rebuilt host.
# Each dataset is sent independently: a full stream at START_SUFFIX, followed
# by every snapshot through END_SUFFIX. Interrupted streams and clean stops
# between streams are resumable.
#
# The replica holder has local receive overrides (readonly=on, mountpoint=none,
# canmount=noauto). `zfs send -bp` sends the original received properties while
# excluding those holder-local overrides. The requested target root mountpoint
# is applied after every dataset reaches the endpoint.
#
# Usage: zfs_backup_restore TARGET_SSH REPLICA_DATASET START_SUFFIX END_SUFFIX TARGET_DATASET MOUNTPOINT
#   zfs_backup_restore ak@10.123.0.3 tank/pug/rpool/services bak-20260701020000 bak-20260710235959 rpool/services /mnt/services

usage() {
  echo >&2 "Usage: zfs_backup_restore TARGET_SSH REPLICA_DATASET START_SUFFIX END_SUFFIX TARGET_DATASET MOUNTPOINT"
  exit 2
}

if (($# != 6)); then
  usage
fi

TARGET_SSH=$1
REPLICA_DATASET=$2
START_SUFFIX=$3
END_SUFFIX=$4
TARGET_DATASET=$5
MOUNTPOINT=$6

# Values interpolated into remote commands are restricted to shell-safe ZFS
# name characters before any SSH call.
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
if [[ ! $START_SUFFIX =~ ^bak-[0-9]{14}$ || ! $END_SUFFIX =~ ^bak-[0-9]{14}$ ]]; then
  echo >&2 "ERROR: snapshot suffixes must match bak-YYYYMMDDhhmmss"
  exit 2
fi

if ! replica_tree=$(zfs list -r -H -o name "$REPLICA_DATASET"); then
  echo >&2 "ERROR: replica dataset does not exist: $REPLICA_DATASET"
  exit 1
fi

source_snapshots() {
  local dataset=$1 snapshot_names snapshot_name suffix in_range=false
  if ! snapshot_names=$(zfs list -H -d 1 -t snapshot -o name -s createtxg "$dataset"); then
    echo >&2 "ERROR: cannot list snapshots on $dataset"
    return 1
  fi

  while IFS= read -r snapshot_name; do
    if [[ $snapshot_name != "$dataset@"* ]]; then
      continue
    fi
    suffix=${snapshot_name#*@}
    if [[ ! $suffix =~ ^[A-Za-z0-9_.:-]+$ ]]; then
      echo >&2 "ERROR: unsupported snapshot name: $snapshot_name"
      return 1
    fi
    if [ "$suffix" = "$START_SUFFIX" ]; then
      in_range=true
    fi
    if [ "$in_range" = true ]; then
      printf '%s\n' "$suffix"
    fi
    if [ "$suffix" = "$END_SUFFIX" ]; then
      if [ "$in_range" = false ]; then
        echo >&2 "ERROR: starting snapshot is newer than target snapshot on $dataset"
        return 1
      fi
      return
    fi
  done <<<"$snapshot_names"

  echo >&2 "ERROR: snapshot range is incomplete on $dataset"
  return 1
}

declare -A SOURCE_SNAPSHOT_LISTS=()
while IFS= read -r dataset; do
  if [[ ! $dataset =~ ^[A-Za-z0-9_.:/-]+$ ]]; then
    echo >&2 "ERROR: unsupported dataset name: $dataset"
    exit 1
  fi
  if [ "$(zfs get -H -o value origin "$dataset")" != - ]; then
    echo >&2 "ERROR: clone datasets are not supported: $dataset"
    exit 1
  fi
  if ! snapshot_list=$(source_snapshots "$dataset"); then
    exit 1
  fi
  SOURCE_SNAPSHOT_LISTS["$dataset"]=$snapshot_list
done <<<"$replica_tree"

# A multiplexed session inherits the master connection's cipher and compression.
# Use a dedicated connection for the already-compressed ZFS stream.
SSH_BULK_OPTIONS=(
  -o ControlPath=none
  -o Compression=no
  -o 'Ciphers=^aes128-gcm@openssh.com'
)

preview_tree() {
  local dataset
  while IFS= read -r dataset; do
    f_trace sudo zfs send -nPbpvc "${dataset}@${START_SUFFIX}"
    if [ "$START_SUFFIX" != "$END_SUFFIX" ]; then
      f_trace sudo zfs send -nPbpvc -I "@${START_SUFFIX}" "${dataset}@${END_SUFFIX}"
    fi
  done <<<"$replica_tree"
}

remote_dataset_state() {
  local target_dataset=$1
  # target_dataset is derived from the validated replica tree.
  # shellcheck disable=SC2029
  ssh "$TARGET_SSH" "sudo bash -s -- '$target_dataset'" <<'REMOTE'
set -euo pipefail
target=$1
token=$(zfs get -H -o value receive_resume_token "$target")
printf 'token\t%s\n' "$token"

# zfs list exits non-zero when the existing dataset has no snapshots yet.
if snapshots=$(zfs list -H -d 1 -t snapshot -o name -s createtxg "$target" 2>/dev/null); then
  while IFS= read -r snapshot; do
    if [[ $snapshot != "$target@"* ]]; then
      continue
    fi
    guid=$(zfs get -H -p -o value guid "$snapshot")
    printf 'snapshot\t%s\t%s\n' "${snapshot#*@}" "$guid"
  done <<<"$snapshots"
fi
REMOTE
}

declare -A TARGET_COUNTS=() TARGET_TOKENS=()
resume_count=0
completed_tree=true

inspect_target_dataset() {
  local source_dataset=$1 target_dataset=$2 state token row_kind suffix remote_guid
  local expected_suffix expected_guid resume_preview resume_suffix
  local -a expected_snapshots=()
  local received_count=0

  mapfile -t expected_snapshots <<<"${SOURCE_SNAPSHOT_LISTS[$source_dataset]}"
  if ! grep -Fxq "$target_dataset" <<<"$remote_datasets"; then
    TARGET_COUNTS["$target_dataset"]=0
    TARGET_TOKENS["$target_dataset"]=''
    completed_tree=false
    return
  fi

  if ! state=$(remote_dataset_state "$target_dataset"); then
    echo >&2 "ERROR: failed to inspect $target_dataset on $TARGET_SSH"
    return 1
  fi
  token=${state%%$'\n'*}
  token=${token#*$'\t'}

  while IFS=$'\t' read -r row_kind suffix remote_guid; do
    if [ "$row_kind" != snapshot ]; then
      continue
    fi
    expected_suffix=${expected_snapshots[$received_count]:-}
    if [ -z "$expected_suffix" ] || [ "$suffix" != "$expected_suffix" ]; then
      echo >&2 "ERROR: $target_dataset is not a prefix of the requested snapshot range"
      return 1
    fi
    expected_guid=$(zfs get -H -p -o value guid "${source_dataset}@${suffix}")
    if [ "$remote_guid" != "$expected_guid" ]; then
      echo >&2 "ERROR: snapshot GUID mismatch for ${target_dataset}@${suffix}"
      return 1
    fi
    ((received_count += 1))
  done <<<"$state"

  if [ "$token" = - ]; then
    token=''
    if ((received_count == 0)); then
      echo >&2 "ERROR: $target_dataset exists without requested snapshots or a resume token"
      return 1
    fi
  else
    if [[ ! $token =~ ^[A-Za-z0-9_-]+$ ]] || ((received_count >= ${#expected_snapshots[@]})); then
      echo >&2 "ERROR: invalid receive resume state on $target_dataset"
      return 1
    fi
    if ! resume_preview=$(sudo zfs send -nP -t "$token" 2>&1); then
      echo >&2 "ERROR: receive resume token on $target_dataset is unusable"
      return 1
    fi
    resume_suffix=$(sed -n 's/.*toname = .*@//p' <<<"$resume_preview" | tail -1)
    if [ "$resume_suffix" != "${expected_snapshots[$received_count]}" ]; then
      echo >&2 "ERROR: receive resume token on $target_dataset is outside the requested range"
      return 1
    fi
    ((resume_count += 1))
  fi

  TARGET_COUNTS["$target_dataset"]=$received_count
  TARGET_TOKENS["$target_dataset"]=$token
  if ((received_count != ${#expected_snapshots[@]})) || [ -n "$token" ]; then
    completed_tree=false
  fi
}

sync_dataset() {
  local source_dataset=$1 target_dataset=$2 token suffix
  local -a snapshots=() send_args=()
  local index=${TARGET_COUNTS[$target_dataset]}

  mapfile -t snapshots <<<"${SOURCE_SNAPSHOT_LISTS[$source_dataset]}"
  token=${TARGET_TOKENS[$target_dataset]}
  if [ -n "$token" ]; then
    echo "Resuming an interrupted restore into $target_dataset"
    f_trace sudo zfs send -nP -t "$token"
    # target_dataset is derived from the validated replica tree.
    # shellcheck disable=SC2029
    sudo zfs send -t "$token" |
      mbuffer -m 256M |
      ssh "${SSH_BULK_OPTIONS[@]}" "$TARGET_SSH" "sudo zfs recv -su '$target_dataset'"
    ((index += 1))
  fi

  while ((index < ${#snapshots[@]})); do
    suffix=${snapshots[$index]}
    send_args=(-bpcv)
    if ((index > 0)); then
      send_args+=(-i "@${snapshots[$((index - 1))]}")
    fi
    send_args+=("${source_dataset}@${suffix}")
    echo "Sending ${source_dataset}@${suffix} -> $target_dataset"
    # target_dataset is derived from the validated replica tree.
    # shellcheck disable=SC2029
    sudo zfs send "${send_args[@]}" |
      mbuffer -m 256M |
      ssh "${SSH_BULK_OPTIONS[@]}" "$TARGET_SSH" "sudo zfs recv -su '$target_dataset'"
    ((index += 1))
  done
}

preview_tree

echo
echo "Restoring ${REPLICA_DATASET}@${START_SUFFIX}..${END_SUFFIX} -> $TARGET_SSH as $TARGET_DATASET"
if ! read -r -p "Proceed? [y/N] " REPLY || [ "$REPLY" != y ]; then
  echo >&2 "Aborted"
  exit 1
fi

# Inspect once immediately before transfer. recv without -F remains the race
# guard if the destination changes after this point.
if ! remote_datasets=$(ssh -n "$TARGET_SSH" 'sudo zfs list -H -o name'); then
  echo >&2 "ERROR: cannot enumerate datasets on $TARGET_SSH"
  exit 1
fi

while IFS= read -r remote_dataset; do
  if [ "$remote_dataset" != "$TARGET_DATASET" ] && [[ $remote_dataset != "$TARGET_DATASET/"* ]]; then
    continue
  fi
  source_dataset=${REPLICA_DATASET}${remote_dataset#"$TARGET_DATASET"}
  if ! grep -Fxq "$source_dataset" <<<"$replica_tree"; then
    echo >&2 "ERROR: unexpected dataset below $TARGET_DATASET on $TARGET_SSH: $remote_dataset"
    exit 1
  fi
done <<<"$remote_datasets"

while IFS= read -r source_dataset; do
  target_dataset=${TARGET_DATASET}${source_dataset#"$REPLICA_DATASET"}
  inspect_target_dataset "$source_dataset" "$target_dataset"
done <<<"$replica_tree"

if ((resume_count > 1)); then
  echo >&2 "ERROR: multiple receive resume tokens exist below $TARGET_DATASET"
  exit 1
fi
if [ "$completed_tree" = true ]; then
  echo >&2 "ERROR: $TARGET_DATASET already contains the requested snapshot range -- refusing to overwrite it"
  exit 1
fi

while IFS= read -r source_dataset; do
  target_dataset=${TARGET_DATASET}${source_dataset#"$REPLICA_DATASET"}
  sync_dataset "$source_dataset" "$target_dataset"
done <<<"$replica_tree"

# Do not make an incomplete tree writable.
while IFS= read -r source_dataset; do
  target_dataset=${TARGET_DATASET}${source_dataset#"$REPLICA_DATASET"}
  expected_guid=$(zfs get -H -p -o value guid "${source_dataset}@${END_SUFFIX}")
  # target_dataset and END_SUFFIX are constrained to shell-safe characters.
  # shellcheck disable=SC2029
  remote_guid=$(ssh -n "$TARGET_SSH" \
    "sudo zfs get -H -p -o value guid '${target_dataset}@${END_SUFFIX}' 2>/dev/null" || true)
  if [ "$remote_guid" != "$expected_guid" ]; then
    echo >&2 "ERROR: restore incomplete: ${target_dataset}@${END_SUFFIX} is missing"
    exit 1
  fi
done <<<"$replica_tree"

# Both arguments are constrained to shell-safe characters above.
# shellcheck disable=SC2029
ssh "$TARGET_SSH" "sudo bash -s -- '$TARGET_DATASET' '$MOUNTPOINT'" <<'REMOTE'
set -euo pipefail
target=$1
mountpoint=$2

zfs set readonly=off canmount=on mountpoint="$mountpoint" "$target"

datasets=$(zfs list -r -H -o name "$target")
while IFS= read -r dataset; do
  dataset_mountpoint=$(zfs get -H -o value mountpoint "$dataset")
  dataset_canmount=$(zfs get -H -o value canmount "$dataset")
  dataset_mounted=$(zfs get -H -o value mounted "$dataset")
  if [ "$dataset_mountpoint" != none ] && [ "$dataset_mountpoint" != legacy ] && [ "$dataset_canmount" != off ] && [ "$dataset_mounted" = no ]; then
    zfs mount "$dataset"
  fi
done <<<"$datasets"

# Received-source tags are skipped by the local snapshot picker.
backup_rows=$(zfs get -t filesystem -r -H -o name,value autobackup:bak "$target")
while IFS=$'\t' read -r dataset value; do
  if [ "$value" = true ]; then
    zfs set autobackup:bak=true "$dataset"
  fi
done <<<"$backup_rows"

echo
zfs list -r -o name,mountpoint,mounted,readonly "$target"
zfs get -t filesystem -r -o name,value,source autobackup:bak "$target"
REMOTE

echo "Restore complete. Converge the host next -- it re-asserts the remaining dataset properties."
