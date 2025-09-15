#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

POOL_REGEX=${1:-}
SNAPSHOT_PREFIX=${2:-}

if [ -z "$POOL_REGEX" ] || [ -z "$SNAPSHOT_PREFIX" ]; then
  f_fail "Usage: zfs_cull POOL_REGEX SNAPSHOT_PREFIX"
fi

zfs list -t snapshot | grep "$POOL_REGEX" | grep "$SNAPSHOT_PREFIX" | cut -d' ' -f1 | sort | uniq | xargs -r -t -n1 zfs destroy
