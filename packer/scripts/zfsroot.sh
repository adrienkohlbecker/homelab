#!/bin/bash

# Unofficial bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eux
set -o pipefail
IFS=$'\n\t'

export DEBIAN_FRONTEND=noninteractive

apt-get -y update >/dev/null
apt-get -y install zfsutils-linux parted rsync >/dev/null

zpool export rpool
zpool import -d /dev rpool

mkdir /tmp/oldroot
mount --bind / /tmp/oldroot

rsync -aPX /tmp/oldroot/. /mirror/.

cat <<EOF > /mirror/etc/fstab
# /etc/fstab: static file system information.
#
# Use 'blkid' to print the universally unique identifier for a
# device; this may be used with UUID= as a more robust way to name devices
# that works even if disks are added and removed. See fstab(5).
#
# <file system> <mount point>   <type>  <options>       <dump>  <pass>

LABEL=boot /boot ext4 defaults 0 1
EOF

sed -i 's|^GRUB_CMDLINE_LINUX=""|GRUB_CMDLINE_LINUX="root=ZFS=rpool/ROOT/ubuntu-1"|' /mirror/etc/default/grub

mount /dev/sda1 /mirror/boot

for d in proc sys dev; do mount --bind /$d /mirror/$d; done

chroot /mirror/ grub-install /dev/sda
chroot /mirror/ update-grub
