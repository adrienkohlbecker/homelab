#!/bin/bash
set -euxo pipefail

case $PACKER_BUILD_NAME in
ubuntu-box)
  HOSTNAME=packer-box
  DISKS=(/dev/disk/by-id/nvme-VMware_Virtual_NVMe_Disk_VMware_NVME_0000_2)
  LAYOUT=""
  ;;
ubuntu-lab)
  HOSTNAME=packer-lab
  DISKS=(/dev/disk/by-id/nvme-VMware_Virtual_NVMe_Disk_VMware_NVME_0000_2 /dev/disk/by-id/nvme-VMware_Virtual_NVMe_Disk_VMware_NVME_0000_3 /dev/disk/by-id/nvme-VMware_Virtual_NVMe_Disk_VMware_NVME_0000_4)
  LAYOUT="mirror"
  ;;
*)
  echo >&2 "Unknown build $PACKER_BUILD_NAME"
  exit 1
  ;;
esac

export DEBIAN_FRONTEND=noninteractive

ARCH=arm64      # amd64
ARCH_GRUB=arm64 # x86_64
USERNAME=vagrant
PASSWORD=vagrant
SSH_KEY_PUB=$(cat /home/vagrant/.ssh/authorized_keys)

apt-get update
apt-get install --yes debootstrap gdisk zfsutils-linux net-tools
systemctl stop zed

for disk in "${DISKS[@]}"; do
  wipefs -a "$disk"
  blkdiscard -f "$disk" || true
  sgdisk --zap-all "$disk"

  sync
  sleep 2

  sgdisk -n1:1M:+512M -t1:EF00 "$disk"       # EFI
  sgdisk -a1 -n5:24K:+1000K -t5:EF02 "$disk" # MBR booting
  sgdisk -n2:0:+500M -t2:8200 "$disk"        # Swap
  sgdisk -n3:0:+2G -t3:BE00 "$disk"          # bpool
  sgdisk -n4:0:0 -t4:BF00 "$disk"            # rpool

  sync
  sleep 2
done

mkdir -p /chroot

zpool create \
  -o ashift=12 \
  -o autotrim=on \
  -o cachefile=/etc/zfs/zpool.cache \
  -o compatibility=grub2 \
  -o feature@livelist=enabled \
  -o feature@zpool_checkpoint=enabled \
  -O devices=off \
  -O acltype=posixacl -O xattr=sa \
  -O compression=lz4 \
  -O normalization=formD \
  -O relatime=on \
  -O canmount=off -O mountpoint=/boot -R /chroot \
  bpool $LAYOUT "${DISKS[@]/%/-part3}"

zpool create \
  -o ashift=12 \
  -o autotrim=on \
  -O acltype=posixacl -O xattr=sa -O dnodesize=auto \
  -O compression=lz4 \
  -O normalization=formD \
  -O relatime=on \
  -O canmount=off -O mountpoint=/ -R /chroot \
  rpool $LAYOUT "${DISKS[@]/%/-part4}"

sync
sleep 2

zfs create -o canmount=off -o mountpoint=none rpool/ROOT
zfs create -o canmount=off -o mountpoint=none bpool/BOOT

zfs create -o mountpoint=/ rpool/ROOT/jammy

zfs create -o mountpoint=/boot bpool/BOOT/jammy

# run this in a separate mount namespace to fix inability to export rpool ("pool is busy")
unshare --mount env DISK="$DISKS" HOSTNAME="$HOSTNAME" PASSWORD="$PASSWORD" ARCH="$ARCH" ARCH_GRUB="$ARCH_GRUB" USERNAME="$USERNAME" SSH_KEY_PUB="$SSH_KEY_PUB" bash </home/vagrant/namespace.sh

mount | grep -v zfs | tac | awk '/\/chroot/ {print $3}' |
  xargs -i{} umount -lf {}
zpool export -a
