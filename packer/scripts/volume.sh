#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get -y update
apt-get -y install parted

# /dev/sda and /dev/sdb are the first disk on the sata controller => the install is done there

ls -al /dev/disk/by-id

# ssds
zfs create -o mountpoint=/mnt/services rpool/services
zfs create -o mountpoint=/mnt/vms/ssd -o recordsize=4k rpool/vms
zfs create -o mountpoint=/mnt/scratch rpool/scratch

zfs create -V 20gb rpool/docker
mkfs.ext4 /dev/zvol/rpool/docker

# data
zpool create -f -o ashift=12 -O compression=lz4 -O casesensitivity=insensitive -O normalization=formD -O mountpoint=none -O atime=off -O xattr=sa \
  data raidz2 /dev/sdc /dev/sdd /dev/sde /dev/sdf
zfs create -o mountpoint=/mnt/data data/data
zfs create -o mountpoint=none data/pictures
zfs create -o mountpoint=none data/arq
zfs create -o mountpoint=/mnt/arq/adrien data/arq/adrien
zfs create -o mountpoint=/mnt/arq/marie data/arq/marie
zfs create -o mountpoint=/mnt/arq/game data/arq/game
zfs create -o mountpoint=/mnt/brumath data/brumath
zfs create -o mountpoint=/mnt/eckwersheim data/eckwersheim
zfs create -o mountpoint=none data/videos
zfs create -o mountpoint=/mnt/media data/media

zpool export data
zpool import -d /dev/disk/by-id data

apt-get -y remove --auto-remove parted
