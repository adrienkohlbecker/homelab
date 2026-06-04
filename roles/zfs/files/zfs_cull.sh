#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

# Operator-only manual tooling: nothing in the repo invokes this (no timer, no
# role). Scheduled snapshot retention is owned by zfs-autobackup's --keep-*
# policy; this is a hand-run helper for ad-hoc culling. Because it drives an
# irreversible `zfs destroy`, it prints the resolved target list and requires
# confirmation (interactive y/N, or a literal --yes third argument).
POOL_REGEX=${1:-}
SNAPSHOT_PREFIX=${2:-}
CONFIRM=${3:-}

if [ -z "$POOL_REGEX" ] || [ -z "$SNAPSHOT_PREFIX" ]; then
  echo >&2 "Usage: zfs_cull POOL_REGEX SNAPSHOT_PREFIX [--yes]"
  exit 1
fi

# `-H -o name` emits exactly the snapshot-name column (tab-stripped, no header),
# so there is no human-table whitespace to mis-split on -- ZFS permits spaces in
# dataset names, and a `cut -d' '` + bare `xargs` would word-split such a name
# into multiple destroy targets. POOL_REGEX is a deliberate extended regex
# (operator-supplied) anchored to the pool/dataset component at the start of the
# name (`^(regex)[/@]`): a bare `tank` then selects only tank's snapshots, never
# `rpool/banktank@...` that merely contains the substring. SNAPSHOT_PREFIX is
# matched as a fixed string anchored to the snapshot component (`@prefix`) -- `@`
# is a unique separator in ZFS names, so it can only select snapshots whose name
# starts with it. `|| true` keeps a no-match grep (exit 1) from tripping the
# errexit/ERR-trap inherited from functions.sh.
mapfile -t targets < <(
  zfs list -H -o name -t snapshot |
    { grep -E "^(${POOL_REGEX})[/@]" || true; } |
    { grep -F -- "@${SNAPSHOT_PREFIX}" || true; } |
    sort -u
)

if [ "${#targets[@]}" -eq 0 ]; then
  echo "No snapshots match POOL_REGEX=${POOL_REGEX} SNAPSHOT_PREFIX=${SNAPSHOT_PREFIX}"
  exit 0
fi

echo "The following ${#targets[@]} snapshot(s) will be destroyed:"
printf "  %s\n" "${targets[@]}"

if [ "$CONFIRM" != "--yes" ]; then
  if [ ! -t 0 ]; then
    echo >&2 "Refusing to destroy without --yes when stdin is not a terminal."
    exit 1
  fi
  read -r -p "Destroy these ${#targets[@]} snapshot(s)? [y/N] " reply
  case "$reply" in
    y | Y | yes | YES) ;;
    *)
      echo "Aborted."
      exit 0
      ;;
  esac
fi

# -d '\n' keeps each whole name as one argument; -t echoes each destroy.
printf "%s\n" "${targets[@]}" | xargs -r -t -d '\n' -n1 zfs destroy
