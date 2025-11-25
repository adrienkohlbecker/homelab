#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

filesystem=$1
cachefile=$2

if [ -z "$filesystem" ] || [ -z "$cachefile" ]; then
  f_fail "Usage: zfs_cull FILESYSTEM CACHEFILE"
fi
if [ ! -f "/etc/zfs/zfs-list.cache/$cachefile" ]; then
  f_fail "Cache file '$cachefile' does not exist"
fi

canmount=$(zfs get -o value -pH canmount $filesystem)
before=$(md5sum /etc/zfs/zfs-list.cache/$cachefile | cut -f 1 -d " ")
zfs set canmount=$canmount $filesystem
sleep 2
after=$(md5sum /etc/zfs/zfs-list.cache/$cachefile | cut -f 1 -d " ")

if [ "$before" != "$after" ]; then
  echo "file updated"
fi
