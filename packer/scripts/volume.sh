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
sudo zpool create -f -o ashift=12 -O compression=lz4 -O mountpoint=none \
  rpool mirror /dev/sda /dev/sdb
sudo zfs create -o mountpoint=none rpool/ROOT
sudo zfs create -o mountpoint=/mirror rpool/ROOT/ubuntu-1
sudo zfs create -o mountpoint=/mnt/docker rpool/docker
sudo zfs create -o mountpoint=/var/lib/libvirt/images rpool/vms

# Tank
sudo zpool create -f -o ashift=12 -O compression=lz4 -O casesensitivity=insensitive -O normalization=formD -O mountpoint=none \
  tank raidz2 /dev/sdc /dev/sdd /dev/sde /dev/sdf /dev/sdg /dev/sdh
sudo zfs create -o mountpoint=/mnt/legacy tank/legacy
sudo zfs create -o mountpoint=/mnt/pictures tank/pictures
sudo zfs create -o mountpoint=/mnt/timemachine tank/timemachine
sudo zfs create -o mountpoint=/mnt/videos tank/videos
sudo zfs create -o mountpoint=/mnt/vms tank/vms

# Media
parted -s /dev/sdj -- mklabel gpt
parted -s /dev/sdj -- mkpart primary 0% 100%
mkfs.ext4 -L snapraid_d1 /dev/sdj1

parted -s /dev/sdk -- mklabel gpt
parted -s /dev/sdk -- mkpart primary 0% 100%
mkfs.ext4 -L snapraid_d2 /dev/sdk1

parted -s /dev/sdl -- mklabel gpt
parted -s /dev/sdl -- mkpart primary 0% 100%
mkfs.ext4 -L snapraid_d3 /dev/sdl1

parted -s /dev/sdm -- mklabel gpt
parted -s /dev/sdm -- mkpart primary 0% 100%
mkfs.ext4 -L snapraid_p1 /dev/sdm1

sudo apt-get -y remove zfsutils-linux parted >/dev/null
