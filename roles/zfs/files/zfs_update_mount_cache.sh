#!/bin/bash
set -euo pipefail
((EUID == 0)) || {
  echo >&2 "Error: I require root"
  exit 1
}

pool=${1:-}

if [ -z "$pool" ]; then
  echo >&2 "Usage: zfs_update_mount_cache POOL"
  exit 1
fi
if [[ ! "$pool" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo >&2 "Invalid pool name: $pool"
  exit 1
fi
cache_file="/etc/zfs/zfs-list.cache/$pool"
if [ ! -f "$cache_file" ]; then
  echo >&2 "Cache file '$pool' does not exist"
  exit 1
fi

canmount=$(zfs get -o value -pH canmount "$pool")
before=$(md5sum "$cache_file" | cut -f 1 -d " ")
zfs set canmount="$canmount" "$pool"

# zed's history_event-zfs-list-cacher.sh regenerates this cache file
# asynchronously in response to the canmount-set above. Poll for the content
# to settle instead of burning a fixed `sleep 2` once per pool on every
# converge. `prev` seeds to a sentinel no md5 can equal, so the loop exits only
# after two *consecutive* reads agree -- ~0.4s in steady state. Seeding `prev`
# to `$before` (as before) treated an unchanged first read as already settled
# and broke on iteration 1, under-reporting a hook that rewrites the file a
# little later. Cache correctness does not depend on the wait -- zed regenerates
# it regardless; the loop only lets us report an accurate changed status below.
# Cap at 5s (25 * 0.2s) so a pathologically slow hook can't hang the play.
prev=initial
after=$before
for _ in $(seq 1 25); do
  sleep 0.2
  after=$(md5sum "$cache_file" | cut -f 1 -d " ")
  if [ "$after" != "$prev" ]; then
    prev=$after
    continue
  fi
  break
done

if [ "$before" != "$after" ]; then
  echo "file updated"
fi
