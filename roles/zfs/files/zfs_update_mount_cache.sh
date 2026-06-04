#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

filesystem=$1
cachefile=$2

if [ -z "$filesystem" ] || [ -z "$cachefile" ]; then
  echo >&2 "Usage: zfs_update_mount_cache FILESYSTEM CACHEFILE"
  exit 1
fi
if [[ ! "$filesystem" =~ ^[a-zA-Z0-9_./-]+$ ]]; then
  echo >&2 "Invalid filesystem name: $filesystem"
  exit 1
fi
if [[ ! "$cachefile" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo >&2 "Invalid cachefile name: $cachefile"
  exit 1
fi
if [ ! -f "/etc/zfs/zfs-list.cache/$cachefile" ]; then
  echo >&2 "Cache file '$cachefile' does not exist"
  exit 1
fi

canmount=$(zfs get -o value -pH canmount "$filesystem")
before=$(md5sum "/etc/zfs/zfs-list.cache/$cachefile" | cut -f 1 -d " ")
zfs set canmount="$canmount" "$filesystem"

# zed's history_event-zfs-list-cacher.sh regenerates this cache file
# asynchronously in response to the canmount-set above. Poll for the
# content to settle instead of burning a fixed `sleep 2` once per pool on
# every converge: in steady state the hook rewrites identical bytes within
# a few hundred ms, so this exits after ~0.2s. Cache correctness does not
# depend on the wait -- zed regenerates it regardless; the loop only gives
# the hook a moment and lets us report an accurate changed status below.
# Cap at 5s (25 * 0.2s) so a pathologically slow hook can't hang the play.
prev=$before
after=$before
for _ in $(seq 1 25); do
  sleep 0.2
  after=$(md5sum "/etc/zfs/zfs-list.cache/$cachefile" | cut -f 1 -d " ")
  if [ "$after" != "$prev" ]; then
    prev=$after
    continue
  fi
  break
done

if [ "$before" != "$after" ]; then
  echo "file updated"
fi
