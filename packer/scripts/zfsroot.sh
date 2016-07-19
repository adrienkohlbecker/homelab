#!/bin/bash

# Unofficial bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eux
set -o pipefail
IFS=$'\n\t'

export DEBIAN_FRONTEND=noninteractive

apt-get -y update
apt-get -y install zfsutils-linux zfs-initramfs parted rsync
modprobe zfs

# SSDs
parted -a optimal -s /dev/sdb -- mklabel msdos
parted -a optimal -s /dev/sdb -- mkpart primary zfs 1MB -512MB
parted -a optimal -s /dev/sdb -- mkpart primary ext2 -512MB 100%
parted -a optimal -s /dev/sdb -- set 2 boot on
parted -a optimal -s /dev/sdb -- print
sleep 2

parted -a optimal -s /dev/sdc -- mklabel msdos
parted -a optimal -s /dev/sdc -- mkpart primary zfs 1MB -512MB
parted -a optimal -s /dev/sdc -- mkpart primary ext2 -512MB 100%
parted -a optimal -s /dev/sdc -- set 2 boot on
parted -a optimal -s /dev/sdc -- print
sleep 2

mkfs.ext2 -L boot /dev/sdb2
mkfs.ext2 -L boot2 /dev/sdc2

zpool create -f -o ashift=12 -O compression=lz4 -O mountpoint=none \
  rpool mirror /dev/sdb1 /dev/sdc1

zpool export rpool
zpool import -o altroot=/mirror -d /dev/disk/by-id rpool

zfs create -o mountpoint=none rpool/ROOT
zfs create -o mountpoint=/ rpool/ROOT/ubuntu-1

sed -i 's|^GRUB_CMDLINE_LINUX=""|GRUB_CMDLINE_LINUX="root=ZFS=rpool/ROOT/ubuntu-1 boot=zfs rpool=rpool bootfs=rpool/ROOT/ubuntu-1"|' /etc/default/grub

rsync --one-file-system -aAXHW / /mirror/

rm -rf /mirror/boot
mkdir /mirror/boot

cat <<EOF > /mirror/etc/fstab
# /etc/fstab: static file system information.
#
# Use 'blkid' to print the universally unique identifier for a
# device; this may be used with UUID= as a more robust way to name devices
# that works even if disks are added and removed. See fstab(5).
#
# <file system> <mount point>   <type>  <options>       <dump>  <pass>
LABEL=boot /boot ext2 defaults 0 0
EOF

mount /dev/sdb2 /mirror/boot
rsync --one-file-system -aAXHW /boot/ /mirror/boot/
umount /boot
umount /mirror/boot
mount /dev/sdb2 /boot

update-grub
grub-install /dev/sdb

blkid
cat /boot/grub/grub.cfg

apt-get -y remove parted rsync
