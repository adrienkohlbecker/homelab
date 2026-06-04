#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

POOL_REGEX=${1:-}
SNAPSHOT_PREFIX=${2:-}

if [ -z "$POOL_REGEX" ] || [ -z "$SNAPSHOT_PREFIX" ]; then
  echo >&2 "Usage: zfs_cull POOL_REGEX SNAPSHOT_PREFIX"
  exit 1
fi

# `-H -o name` emits exactly the snapshot-name column (tab-stripped, no header),
# so there is no human-table whitespace to mis-split on -- ZFS permits spaces in
# dataset names, and the old `cut -d' '` + bare `xargs` would word-split such a
# name into multiple destroy targets. POOL_REGEX is a deliberate extended regex
# (operator-supplied); SNAPSHOT_PREFIX is matched as a fixed string anchored to
# the snapshot component (`@prefix`) so it can only select snapshots whose name
# starts with it, never a substring elsewhere on the line. `-d '\n'` keeps each
# whole name as one argument.
zfs list -H -o name -t snapshot | grep -E "$POOL_REGEX" | grep -F -- "@$SNAPSHOT_PREFIX" | sort -u | xargs -r -t -d '\n' -n1 zfs destroy
