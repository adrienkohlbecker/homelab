#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get -y update >/dev/null
apt-get -y install zfsutils-linux parted >/dev/null

# /dev/sda and /dev/sdb are the first disk on the sata controller => the install is done there

ls -al /dev/disk/by-id

# ssds
zfs create -o mountpoint=/mnt/services rpool/services

# Tank
zpool create -f -o ashift=12 -O compression=lz4 -O casesensitivity=insensitive -O normalization=formD -O mountpoint=none \
  tank raidz2 /dev/sdc /dev/sdd /dev/sde /dev/sdf /dev/sdg /dev/sdh
zfs create -o mountpoint=/mnt/legacy tank/legacy
zfs create -o mountpoint=/mnt/pictures tank/pictures
zfs create -o mountpoint=/mnt/arq tank/arq
zfs create -o mountpoint=/mnt/brumath tank/brumath
zfs create -o mountpoint=/mnt/videos tank/videos

zpool export tank
zpool import -d /dev/disk/by-id tank

# Media
parted -s /dev/sdi -- mklabel gpt
parted -s /dev/sdi -- mkpart primary 0% 100%
sleep 2
mkfs.ext4 -L snapraid_d1 /dev/sdi1

parted -s /dev/sdj -- mklabel gpt
parted -s /dev/sdj -- mkpart primary 0% 100%
sleep 2
mkfs.ext4 -L snapraid_d2 /dev/sdj1

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
