#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get -y update
apt-get -y install zfsutils zfs-initramfs

apt-get -y install gdisk

mdadm --zero-superblock --force /dev/sdb || true
mdadm --zero-superblock --force /dev/sdc || true
sgdisk --zap-all /dev/sdb
sgdisk --zap-all /dev/sdc

zpool create -f -o ashift=12 -O compression=lz4 -O mountpoint=none -O atime=off -O normalization=formD -O xattr=sa \
  rpool mirror /dev/sdb /dev/sdc

# create grub partitions (zfs leaves the first 2048 sectors free)
sgdisk -a1 -n2:512:2047 -t2:EF02 /dev/sdb
sgdisk -a1 -n2:512:2047 -t2:EF02 /dev/sdc
sgdisk --print /dev/sdb
sgdisk --print /dev/sdc

apt-get -y remove --auto-remove gdisk

zpool export rpool
zpool import -o altroot=/mirror -d /dev/disk/by-id rpool

zfs create -o mountpoint=none rpool/ROOT
zfs create -o mountpoint=/ -o acltype=posixacl rpool/ROOT/focal

rsync --one-file-system -aAXHW / /mirror/

mount --rbind /dev  /mirror/dev
mount --rbind /proc /mirror/proc
mount --rbind /sys  /mirror/sys

chroot /mirror grub-probe /
chroot /mirror update-initramfs -u -k all
chroot /mirror update-grub
chroot /mirror grub-install /dev/sdb
chroot /mirror grub-install /dev/sdc
chroot /mirror blkid
chroot /mirror cat /boot/grub/grub.cfg
# remove root disk from fstab as we'll remove that disk next
chroot /mirror sed -i '/ext4/d' /etc/fstab

# umount /mirror/dev
# umount /mirror/proc
# umount /mirror/sys
