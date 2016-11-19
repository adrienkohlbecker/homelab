#!/bin/bash

# Unofficial bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eux
set -o pipefail
IFS=$'\n\t'

export DEBIAN_FRONTEND=noninteractive

apt-get -y update >/dev/null
apt-get -y install zfsutils-linux parted >/dev/null

# /dev/sdi and /dev/sdj are the first disk on the sata controller => the install is done there

ls -al /dev/disk/by-id

# ssds
zfs create -o mountpoint=/mnt/services rpool/services
zfs create -o mountpoint=/var/lib/docker rpool/docker
zfs create -o mountpoint=/var/lib/libvirt/images rpool/vms

# Tank
zpool create -f -o ashift=12 -O compression=lz4 -O casesensitivity=insensitive -O normalization=formD -O mountpoint=none \
  tank raidz2 /dev/sda /dev/sdb /dev/sdc /dev/sdd /dev/sde /dev/sdf
zfs create -o mountpoint=/mnt/legacy tank/legacy
zfs create -o mountpoint=/mnt/pictures tank/pictures
zfs create -o mountpoint=/mnt/sftp tank/sftp
zfs create -o mountpoint=/mnt/brumath tank/brumath
zfs create -o mountpoint=/mnt/videos tank/videos
zfs create -o mountpoint=/mnt/vms tank/vms

zpool export tank
zpool import -d /dev/disk/by-id tank

# Media
parted -s /dev/sdg -- mklabel gpt
parted -s /dev/sdg -- mkpart primary 0% 100%
sleep 2
mkfs.ext4 -L snapraid_d1 /dev/sdg1

parted -s /dev/sdh -- mklabel gpt
parted -s /dev/sdh -- mkpart primary 0% 100%
sleep 2
mkfs.ext4 -L snapraid_d2 /dev/sdh1

parted -s /dev/sdk -- mklabel gpt
parted -s /dev/sdk -- mkpart primary 0% 100%
sleep 2
mkfs.ext4 -L snapraid_d3 /dev/sdk1

parted -s /dev/sdl -- mklabel gpt
parted -s /dev/sdl -- mkpart primary 0% 100%
sleep 2
mkfs.ext4 -L snapraid_d4 /dev/sdl1

parted -s /dev/sdm -- mklabel gpt
parted -s /dev/sdm -- mkpart primary 0% 100%
sleep 2
mkfs.ext4 -L snapraid_p1 /dev/sdm1

apt-get -y remove parted >/dev/null
