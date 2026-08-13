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
# Without --from, the recursive send preserves all history through the endpoint.
# With --from, each dataset starts with that snapshot and receives its remaining
# history through the endpoint, excluding every older snapshot.
#
# Interrupted streams with receive tokens are resumable. A cutoff restore spans
# separate streams per dataset, so a token-free partial target is also accepted
# only when its boundary snapshot GUIDs form a valid prefix of the requested
# restore. Every other existing target fails closed.
#
# Usage: zfs_backup_restore [--from SNAPSHOT_SUFFIX] TARGET_SSH REPLICA_DATASET SNAPSHOT_SUFFIX TARGET_DATASET MOUNTPOINT
#   zfs_backup_restore --from bak-20260701020000 ak@10.123.0.3 tank/pug/rpool/services bak-20260710235959 rpool/services /mnt/services

usage() {
  echo >&2 "Usage: zfs_backup_restore [--from SNAPSHOT_SUFFIX] TARGET_SSH REPLICA_DATASET SNAPSHOT_SUFFIX TARGET_DATASET MOUNTPOINT"
  exit 2
}

FROM_SNAPSHOT_SUFFIX=
while (($#)); do
  case $1 in
  --from)
    if (($# < 2)); then
      usage
    fi
    FROM_SNAPSHOT_SUFFIX=$2
    shift 2
    ;;
  --)
    shift
    break
    ;;
  -*)
    usage
    ;;
  *)
    break
    ;;
  esac
done

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
if [[ -n $FROM_SNAPSHOT_SUFFIX && ! $FROM_SNAPSHOT_SUFFIX =~ ^bak-[0-9]{14}$ ]]; then
  echo >&2 "ERROR: --from snapshot suffix must match bak-YYYYMMDDhhmmss"
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
  if [ -n "$FROM_SNAPSHOT_SUFFIX" ]; then
    if ! zfs list -t snapshot -H -o name "${ds}@${FROM_SNAPSHOT_SUFFIX}" >/dev/null; then
      echo >&2 "ERROR: recursive starting snapshot missing: ${ds}@${FROM_SNAPSHOT_SUFFIX}"
      exit 1
    fi
    from_txg=$(zfs get -H -p -o value createtxg "${ds}@${FROM_SNAPSHOT_SUFFIX}")
    target_txg=$(zfs get -H -p -o value createtxg "${ds}@${SNAPSHOT_SUFFIX}")
    if ((from_txg > target_txg)); then
      echo >&2 "ERROR: starting snapshot is newer than target snapshot on $ds"
      exit 1
    fi
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

remote_cutoff_inventory() {
  # A cutoff restore can stop cleanly between streams without leaving a resume
  # token. Report boundary GUIDs so cutoff_state can distinguish that valid
  # partial tree from unrelated pre-existing data.
  # All arguments are constrained to shell-safe ZFS or snapshot characters.
  # shellcheck disable=SC2029
  ssh "$TARGET_SSH" "sudo bash -s -- '$TARGET_DATASET' '$FROM_SNAPSHOT_SUFFIX' '$SNAPSHOT_SUFFIX'" <<'REMOTE'
set -euo pipefail
target=$1
from_suffix=$2
target_suffix=$3

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
  echo present
elif [ "$resume_count" -eq 1 ]; then
  printf 'resume\t%s\t%s\n' "$resume_dataset" "$resume_token"
else
  echo >&2 "ERROR: multiple interrupted receive tokens exist below $target"
  exit 21
fi

while IFS= read -r dataset; do
  # Missing boundary snapshots are expected while a multi-stream restore runs.
  from_guid=$(zfs get -H -p -o value guid "${dataset}@${from_suffix}" 2>/dev/null || true)
  target_guid=$(zfs get -H -p -o value guid "${dataset}@${target_suffix}" 2>/dev/null || true)
  printf 'dataset\t%s\t%s\t%s\n' "$dataset" "${from_guid:--}" "${target_guid:--}"
done < <(zfs list -r -H -o name "$target")
REMOTE
}

cutoff_state() {
  local inventory inventory_header resume_dataset='' resume_token='' resume_fields
  local source_dataset target_dataset source_from_guid source_target_guid
  local row_kind from_guid target_guid all_done=true state_summary='' state
  local -A expected_targets=() remote_from_guids=() remote_target_guids=()

  if ! inventory=$(remote_cutoff_inventory); then
    return 1
  fi
  inventory_header=${inventory%%$'\n'*}
  if [ "$inventory_header" = absent ]; then
    echo absent
    return
  fi
  if [[ $inventory_header == resume$'\t'* ]]; then
    resume_fields=${inventory_header#*$'\t'}
    resume_dataset=${resume_fields%%$'\t'*}
    resume_token=${resume_fields#*$'\t'}
  elif [ "$inventory_header" != present ]; then
    echo >&2 "ERROR: unexpected target inventory from $REMOTE_HOSTNAME: $inventory_header"
    return 1
  fi

  while IFS=$'\t' read -r row_kind target_dataset from_guid target_guid; do
    if [ "$row_kind" != dataset ]; then
      continue
    fi
    if [[ ! $target_dataset =~ ^[A-Za-z0-9][A-Za-z0-9_.:/-]*$ ]] ||
      { [ "$target_dataset" != "$TARGET_DATASET" ] && [[ $target_dataset != "$TARGET_DATASET/"* ]]; }; then
      echo >&2 "ERROR: invalid dataset returned by $REMOTE_HOSTNAME: $target_dataset"
      return 1
    fi
    remote_from_guids["$target_dataset"]=$from_guid
    remote_target_guids["$target_dataset"]=$target_guid
  done <<<"$inventory"

  while IFS= read -r source_dataset; do
    target_dataset=${TARGET_DATASET}${source_dataset#"$REPLICA_DATASET"}
    expected_targets["$target_dataset"]=true
    source_from_guid=$(zfs get -H -p -o value guid "${source_dataset}@${FROM_SNAPSHOT_SUFFIX}")
    source_target_guid=$(zfs get -H -p -o value guid "${source_dataset}@${SNAPSHOT_SUFFIX}")

    if [ "${remote_target_guids[$target_dataset]:--}" = "$source_target_guid" ]; then
      state='done'
    elif [ "${remote_from_guids[$target_dataset]:--}" = "$source_from_guid" ]; then
      state=base
      all_done=false
    elif [ -z "${remote_from_guids[$target_dataset]+set}" ]; then
      state=absent
      all_done=false
    elif [ "$resume_dataset" = "$target_dataset" ] &&
      [ "${remote_from_guids[$target_dataset]}" = - ] &&
      [ "${remote_target_guids[$target_dataset]}" = - ]; then
      state=receiving
      all_done=false
    else
      echo >&2 "ERROR: $target_dataset on $REMOTE_HOSTNAME does not match the requested restore history"
      return 1
    fi
    state_summary+=$'\n'"${target_dataset}"$'\t'"${state}"
  done <<<"$replica_tree"

  for target_dataset in "${!remote_from_guids[@]}"; do
    if [ -z "${expected_targets[$target_dataset]+set}" ]; then
      echo >&2 "ERROR: unexpected dataset below $TARGET_DATASET on $REMOTE_HOSTNAME: $target_dataset"
      return 1
    fi
  done

  if [ -n "$resume_dataset" ]; then
    if [[ ! $resume_dataset =~ ^[A-Za-z0-9][A-Za-z0-9_.:/-]*$ ]] ||
      [ -z "${expected_targets[$resume_dataset]+set}" ]; then
      echo >&2 "ERROR: invalid resume dataset returned by $REMOTE_HOSTNAME"
      return 1
    fi
    if [[ ! $resume_token =~ ^[A-Za-z0-9_-]+$ ]]; then
      echo >&2 "ERROR: invalid receive resume token returned by $REMOTE_HOSTNAME"
      return 1
    fi
    printf 'resume\t%s\t%s%s\n' "$resume_dataset" "$resume_token" "$state_summary"
  elif [ "$all_done" = true ]; then
    printf 'exists%s\n' "$state_summary"
  else
    printf 'partial%s\n' "$state_summary"
  fi
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

latest_common_snapshot() {
  local source_dataset=$1 target_dataset=$2 snapshot_rows snapshot_name snapshot_guid
  local snapshot_suffix source_guid snapshot_txg latest_suffix='' latest_txg=0
  # A newly-created target can have no completed snapshots until resume ends.
  # target_dataset is derived from the validated replica tree.
  # shellcheck disable=SC2029
  snapshot_rows=$(ssh -n "$TARGET_SSH" \
    "sudo zfs get -r -t snapshot -H -p -o name,value guid '$target_dataset' 2>/dev/null" || true)
  while IFS=$'\t' read -r snapshot_name snapshot_guid; do
    if [[ $snapshot_name != "$target_dataset@"* ]]; then
      continue
    fi
    snapshot_suffix=${snapshot_name#*@}
    if [[ ! $snapshot_suffix =~ ^[A-Za-z0-9_.:-]+$ ]]; then
      echo >&2 "ERROR: unsupported snapshot name on $REMOTE_HOSTNAME: $snapshot_name"
      return 1
    fi
    if ! source_guid=$(zfs get -H -p -o value guid "${source_dataset}@${snapshot_suffix}" 2>/dev/null); then
      continue
    fi
    if [ "$source_guid" != "$snapshot_guid" ]; then
      continue
    fi
    snapshot_txg=$(zfs get -H -p -o value createtxg "${source_dataset}@${snapshot_suffix}")
    if ((snapshot_txg > latest_txg)); then
      latest_suffix=$snapshot_suffix
      latest_txg=$snapshot_txg
    fi
  done <<<"$snapshot_rows"
  printf '%s\n' "$latest_suffix"
}

complete_resumed_tree() {
  local source_dataset target_dataset common_suffix
  while IFS= read -r source_dataset; do
    target_dataset=${TARGET_DATASET}${source_dataset#"$REPLICA_DATASET"}
    if ! remote_dataset_exists "$target_dataset"; then
      receive_tree "$source_dataset" "$target_dataset"
      continue
    fi
    if remote_snapshot_matches "$target_dataset" "$SNAPSHOT_SUFFIX" \
      "$(zfs get -H -p -o value guid "${source_dataset}@${SNAPSHOT_SUFFIX}")"; then
      continue
    fi
    common_suffix=$(latest_common_snapshot "$source_dataset" "$target_dataset")
    if [ -z "$common_suffix" ]; then
      echo >&2 "ERROR: no common snapshot for $source_dataset and $target_dataset on $REMOTE_HOSTNAME"
      return 1
    fi
    # target_dataset is derived from the validated replica tree.
    # shellcheck disable=SC2029
    sudo zfs send -bcv -I "${source_dataset}@${common_suffix}" \
      "${source_dataset}@${SNAPSHOT_SUFFIX}" |
      mbuffer -m 256M |
      ssh "${SSH_BULK_OPTIONS[@]}" "$TARGET_SSH" "sudo zfs recv -su '$target_dataset'"
  done <<<"$replica_tree"
}

preview_cutoff_tree() {
  local source_dataset
  while IFS= read -r source_dataset; do
    f_trace sudo zfs send -nbPcv "${source_dataset}@${FROM_SNAPSHOT_SUFFIX}"
    if [ "$FROM_SNAPSHOT_SUFFIX" != "$SNAPSHOT_SUFFIX" ]; then
      f_trace sudo zfs send -nbPcv -I "${source_dataset}@${FROM_SNAPSHOT_SUFFIX}" \
        "${source_dataset}@${SNAPSHOT_SUFFIX}"
    fi
  done <<<"$replica_tree"
}

receive_cutoff_stream() {
  local source_dataset=$1 target_dataset=$2 stream_kind=$3
  if [ "$stream_kind" = full ]; then
    # target_dataset is derived from the validated replica tree.
    # shellcheck disable=SC2029
    sudo zfs send -bcv "${source_dataset}@${FROM_SNAPSHOT_SUFFIX}" |
      mbuffer -m 256M |
      ssh "${SSH_BULK_OPTIONS[@]}" "$TARGET_SSH" "sudo zfs recv -su '$target_dataset'"
  else
    # target_dataset is derived from the validated replica tree.
    # shellcheck disable=SC2029
    sudo zfs send -bcv -I "${source_dataset}@${FROM_SNAPSHOT_SUFFIX}" \
      "${source_dataset}@${SNAPSHOT_SUFFIX}" |
      mbuffer -m 256M |
      ssh "${SSH_BULK_OPTIONS[@]}" "$TARGET_SSH" "sudo zfs recv -su '$target_dataset'"
  fi
}

remote_snapshot_matches() {
  local target_dataset=$1 snapshot_suffix=$2 expected_guid=$3 remote_guid
  # The snapshot is absent until its stream completes, which is a normal state.
  # shellcheck disable=SC2029
  remote_guid=$(ssh -n "$TARGET_SSH" \
    "sudo zfs get -H -p -o value guid '${target_dataset}@${snapshot_suffix}' 2>/dev/null" || true)
  [ "$remote_guid" = "$expected_guid" ]
}

receive_cutoff_tree() {
  local source_dataset target_dataset source_from_guid source_target_guid
  while IFS= read -r source_dataset; do
    target_dataset=${TARGET_DATASET}${source_dataset#"$REPLICA_DATASET"}
    source_from_guid=$(zfs get -H -p -o value guid "${source_dataset}@${FROM_SNAPSHOT_SUFFIX}")
    source_target_guid=$(zfs get -H -p -o value guid "${source_dataset}@${SNAPSHOT_SUFFIX}")

    if remote_snapshot_matches "$target_dataset" "$SNAPSHOT_SUFFIX" "$source_target_guid"; then
      continue
    fi
    if ! remote_snapshot_matches "$target_dataset" "$FROM_SNAPSHOT_SUFFIX" "$source_from_guid"; then
      receive_cutoff_stream "$source_dataset" "$target_dataset" full
    fi
    if [ "$FROM_SNAPSHOT_SUFFIX" != "$SNAPSHOT_SUFFIX" ] &&
      ! remote_snapshot_matches "$target_dataset" "$SNAPSHOT_SUFFIX" "$source_target_guid"; then
      receive_cutoff_stream "$source_dataset" "$target_dataset" incremental
    fi
  done <<<"$replica_tree"
}

REMOTE_HOSTNAME=$(ssh -n "$TARGET_SSH" 'hostnamectl --static')
if [ -n "$FROM_SNAPSHOT_SUFFIX" ]; then
  state_command=cutoff_state
else
  state_command=remote_state
fi
if ! initial_state=$($state_command); then
  echo >&2 "ERROR: failed to inspect $TARGET_DATASET on $REMOTE_HOSTNAME"
  exit 1
fi
initial_state_header=${initial_state%%$'\n'*}

case "$initial_state_header" in
absent)
  echo "Starting a new restore"
  if [ -n "$FROM_SNAPSHOT_SUFFIX" ]; then
    preview_cutoff_tree
  else
    f_trace sudo zfs send -Rnbcv "$SNAP"
  fi
  ;;
partial)
  echo "Continuing a cutoff restore from completed streams"
  preview_cutoff_tree
  ;;
exists)
  echo >&2 "ERROR: $TARGET_DATASET already exists on $REMOTE_HOSTNAME with the requested restore complete -- refusing to overwrite it"
  exit 1
  ;;
resume$'\t'*)
  resume_fields=${initial_state_header#*$'\t'}
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
  if [ -n "$FROM_SNAPSHOT_SUFFIX" ]; then
    preview_cutoff_tree
  fi
  ;;
*)
  echo >&2 "ERROR: unexpected target state from $REMOTE_HOSTNAME: $initial_state"
  exit 1
  ;;
esac

echo
if [ -n "$FROM_SNAPSHOT_SUFFIX" ]; then
  echo "Restoring ${REPLICA_DATASET}@${FROM_SNAPSHOT_SUFFIX}..${SNAPSHOT_SUFFIX} -> $REMOTE_HOSTNAME ($TARGET_SSH) as $TARGET_DATASET"
else
  echo "Restoring $SNAP -> $REMOTE_HOSTNAME ($TARGET_SSH) as $TARGET_DATASET"
fi
if ! read -r -p "Proceed? [y/N] " REPLY || [ "$REPLY" != y ]; then
  echo >&2 "Aborted"
  exit 1
fi

# The preview and prompt may take time. Refuse if anything changed remotely
# before the receive; recv without -F is the final fail-closed race guard.
if ! current_state=$($state_command); then
  echo >&2 "ERROR: failed to re-check $TARGET_DATASET on $REMOTE_HOSTNAME"
  exit 1
fi
if [ "$current_state" != "$initial_state" ]; then
  echo >&2 "ERROR: $TARGET_DATASET changed after preflight -- refusing to continue"
  exit 1
fi

if [ -n "$FROM_SNAPSHOT_SUFFIX" ]; then
  if [[ $initial_state_header == resume$'\t'* ]]; then
    # resume_dataset is constrained to the requested target tree above.
    # shellcheck disable=SC2029
    sudo zfs send -t "$resume_token" |
      mbuffer -m 256M |
      ssh "${SSH_BULK_OPTIONS[@]}" "$TARGET_SSH" "sudo zfs recv -su '$resume_dataset'"
  fi
  receive_cutoff_tree
elif [ "$initial_state_header" = absent ]; then
  receive_tree "$REPLICA_DATASET" "$TARGET_DATASET"
else
  # resume_dataset is constrained to the requested target tree above.
  # shellcheck disable=SC2029
  sudo zfs send -t "$resume_token" |
    mbuffer -m 256M |
    ssh "${SSH_BULK_OPTIONS[@]}" "$TARGET_SSH" "sudo zfs recv -su '$resume_dataset'"

  # A resume token covers one filesystem stream, not later members of a
  # recursive replication package. Continue every dataset from its newest
  # matching snapshot so the requested endpoint cannot be silently omitted.
  complete_resumed_tree
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
