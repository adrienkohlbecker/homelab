#!/bin/bash

# Unofficial bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eux
set -o pipefail
IFS=$'\n\t'

export DEBIAN_FRONTEND=noninteractive

apt-get -y update >/dev/null
apt-get -y install zfsutils-linux parted >/dev/null

# /dev/sdi is the first disk on the sata controller => the install is done there

# SSDs
parted -s /dev/sda -- mklabel msdos
parted -s /dev/sda -- mkpart primary 0% 512MB
parted -s /dev/sda -- mkpart primary 512MB 100%
parted -s /dev/sda -- set 1 boot on
sleep 2

parted -s /dev/sdb -- mklabel msdos
parted -s /dev/sdb -- mkpart primary 0% 512MB
parted -s /dev/sdb -- mkpart primary 512MB 100%
parted -s /dev/sdb -- set 1 boot on
sleep 2

mkfs.ext4 -L boot /dev/sda1
mkfs.ext4 -L boot2 /dev/sdb1

zpool create -f -o ashift=12 -O compression=lz4 -O mountpoint=none \
  rpool mirror /dev/sda2 /dev/sdb2
zfs create -o mountpoint=none rpool/ROOT
zfs create -o mountpoint=/mirror rpool/ROOT/ubuntu-1
zfs create -o mountpoint=/mnt/docker rpool/docker
zfs create -o mountpoint=/var/lib/libvirt/images rpool/vms

# Tank
zpool create -f -o ashift=12 -O compression=lz4 -O casesensitivity=insensitive -O normalization=formD -O mountpoint=none \
  tank raidz2 /dev/sdc /dev/sdd /dev/sde /dev/sdf /dev/sdg /dev/sdh
zfs create -o mountpoint=/mnt/legacy tank/legacy
zfs create -o mountpoint=/mnt/pictures tank/pictures
zfs create -o mountpoint=/mnt/timemachine tank/timemachine
zfs create -o mountpoint=/mnt/videos tank/videos
zfs create -o mountpoint=/mnt/vms tank/vms

zpool export tank
zpool import -d /dev/disk/by-id tank

# Media
parted -s /dev/sdj -- mklabel gpt
parted -s /dev/sdj -- mkpart primary 0% 100%
sleep 2
mkfs.ext4 -L snapraid_d1 /dev/sdj1

parted -s /dev/sdk -- mklabel gpt
parted -s /dev/sdk -- mkpart primary 0% 100%
sleep 2
mkfs.ext4 -L snapraid_d2 /dev/sdk1

parted -s /dev/sdl -- mklabel gpt
parted -s /dev/sdl -- mkpart primary 0% 100%
sleep 2
mkfs.ext4 -L snapraid_d3 /dev/sdl1

parted -s /dev/sdm -- mklabel gpt
parted -s /dev/sdm -- mkpart primary 0% 100%
sleep 2
mkfs.ext4 -L snapraid_p1 /dev/sdm1

apt-get -y remove zfsutils-linux parted >/dev/null
