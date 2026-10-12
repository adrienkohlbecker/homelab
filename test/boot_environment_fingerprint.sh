#!/bin/bash
# Print the shared storage a boot environment install must leave untouched, in
# a form that reads the same from either release's root: partition tables,
# md arrays, the ESP's files, the firmware boot entries, pool layouts and
# health, datasets other than the new root, active swap, and the shared
# rpool/ROOT properties. $1 names the new root, left out of the dataset list.
set -euo pipefail

new_root=$1

echo "== partitions"
for part in /sys/class/block/*/partition; do
  dir=${part%/partition}
  name=${dir##*/}
  echo "$name $(<"$dir/start") $(<"$dir/size") $(lsblk -dnro PARTUUID,PARTTYPE "/dev/$name")"
done

echo "== md arrays"
if [ -r /proc/mdstat ] && grep -q '^md' /proc/mdstat; then
  mdadm --detail --scan | grep -o 'UUID=[^ ]*' | sort
fi

echo "== esp"
# Each release pads findmnt columns differently.
findmnt -no FSTYPE,UUID /boot/efi | awk '{ print $1, $2 }'
find /boot/efi -type f -exec sha256sum {} + | sort -k2

echo "== efi boot entries"
efibootmgr | awk '/^BootOrder/ || /^Boot[0-9A-F]{4}/ { print $1 }'

echo "== pools"
zpool list -H -o name,guid
zpool status -P | awk '/^config:/ { on = 1; next } /^errors:/ { on = 0 } on && NF >= 2 && $1 != "NAME" { print $1, $2 }'

echo "== datasets"
zfs list -H -o name,mountpoint,canmount -t filesystem,volume | awk -v skip="$new_root" '$1 != skip'

# zram devices are recreated with a fresh UUID every boot.
echo "== swap"
swapon --show=NAME,UUID --noheadings | awk '$1 !~ "^/dev/zram" { print $2 }' | sort

echo "== rpool/ROOT"
zfs get -H -o property,value org.zfsbootmenu:commandline rpool/ROOT
