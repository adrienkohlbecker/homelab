#!/bin/bash
# Print the shared state a boot environment install must leave untouched, in a
# form that reads the same from either release's root: partition tables, md
# arrays, the ESP's files, the firmware boot entries, pool layouts and health,
# datasets other than the new root, active swap, the shared rpool/ROOT
# properties, and the host identity the new root carries over. $1 names the
# new root, left out of the dataset list; $2 is the login user.
set -euo pipefail

new_root=$1
user=$2

echo "== partitions"
for part in /sys/class/block/*/partition; do
  dir=${part%/partition}
  name=${dir##*/}
  echo "$name $(<"$dir/start") $(<"$dir/size") $(lsblk -dnro PARTUUID,PARTTYPE "/dev/$name")"
done

# Level, size, and the role of each member, so a degraded or reshaped array
# shows. Keys rather than the full export, which varies between mdadm releases.
echo "== md arrays"
for array in /dev/md/*; do
  [ -e "$array" ] || continue
  echo "$array"
  mdadm --detail --export "$array" |
    awk -F= '$1 ~ /^MD_(LEVEL|DEVICES|UUID)$/ || $1 ~ /^MD_DEVICE_.*_ROLE$/' | sort
done

echo "== esp"
# Each release pads findmnt columns differently.
findmnt -no FSTYPE,UUID /boot/efi | awk '{ print $1, $2 }'
find /boot/efi -type f -exec sha256sum {} + | sort -k2

# The boot order, and each entry's partition and loader: each release's
# efibootmgr renders the same device path differently.
echo "== efi boot entries"
efibootmgr | awk '
  /^BootOrder:/ { print; next }
  /^Boot[0-9A-F][0-9A-F][0-9A-F][0-9A-F]/ {
    disk = match($0, /HD\([^)]*\)/) ? substr($0, RSTART, RLENGTH) : ""
    loader = match($0, /\\EFI\\[^)]*/) ? substr($0, RSTART, RLENGTH) : ""
    print $1, disk, loader
  }'

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

echo "== identity"
hostid
cat /etc/machine-id
for key in /etc/ssh/ssh_host_*_key.pub; do
  ssh-keygen -l -f "$key"
done
id -u "$user"
id -g "$user"
