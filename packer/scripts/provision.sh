#!/bin/bash
set -euxo pipefail

case $PACKER_BUILD_NAME in
ubuntu-pug | ubuntu-box)
  DISKS=(/dev/disk/by-id/nvme-VMware_Virtual_NVMe_Disk_VMware_NVME_0000_2)
  LAYOUT=""
  ;;
ubuntu-lab)
  DISKS=(/dev/disk/by-id/nvme-VMware_Virtual_NVMe_Disk_VMware_NVME_0000_2 /dev/disk/by-id/nvme-VMware_Virtual_NVMe_Disk_VMware_NVME_0000_3 /dev/disk/by-id/nvme-VMware_Virtual_NVMe_Disk_VMware_NVME_0000_4)
  LAYOUT="mirror"
  ;;
*)
  echo >&2 "Unknown build $PACKER_BUILD_NAME"
  exit 1
  ;;
esac

MACHINE=$(uname -m)
case $MACHINE in
aarch64)
  ARCH=arm64
  ARCH_GRUB=arm64
  ;;
x86_64)
  ARCH=amd64
  ARCH_GRUB=x86_64
  ;;
*)
  echo >&2 "Unknown machine name $MACHINE"
  exit 1
  ;;
esac

HOSTNAME=$PACKER_BUILD_NAME
USERNAME=vagrant
PASSWORD=vagrant
SSH_KEY_PUB=$(cat /home/vagrant/.ssh/authorized_keys)

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install --yes debootstrap gdisk zfsutils-linux net-tools
systemctl stop zed

for disk in "${DISKS[@]}"; do
  wipefs -a "$disk"
  blkdiscard -f "$disk" || true
  sgdisk --zap-all "$disk"

  sync
  sleep 2

  sgdisk -n1:1M:+512M -t1:EF00 "$disk"       # EFI (EF00 = EFI system partition)
  sgdisk -a1 -n5:24K:+1000K -t5:EF02 "$disk" # MBR booting (EF02 = BIOS boot partition)

  if [ "$LAYOUT" = "" ]; then
    sgdisk -n2:0:+500M -t2:8200 "$disk" # Swap (8200 = Linux Swap)
  else
    sgdisk -n2:0:+500M -t2:FD00 "$disk" # Swap (FD00 = Linux RAID)
  fi

  sgdisk -n3:0:+2G -t3:BE00 "$disk" # bpool (BE00 = Solaris boot)
  sgdisk -p "$disk"
  if [ "$PACKER_BUILD_NAME" = "ubuntu-lab" ]; then
    sgdisk -n6:-2G:0 -t6:BF01 "$disk" # metadata vdev (BF01 = Solaris /usr & Mac ZFS, default when doing zpool create)
  fi
  sgdisk -n4:0:0 -t4:BF00 "$disk" # rpool (BF00 = Solaris root)

  sgdisk -p "$disk"

  sync
  sleep 2
done

mkdir -p /chroot

zpool create \
  -o ashift=12 \
  -o autotrim=off \
  -o cachefile=/etc/zfs/zpool.cache \
  -o compatibility=grub2 \
  -o feature@livelist=enabled \
  -o feature@zpool_checkpoint=enabled \
  -O acltype=posixacl \
  -O atime=on \
  -O canmount=off \
  -O casesensitivity=sensitive \
  -O compression=lz4 \
  -O normalization=formD \
  -O overlay=off \
  -O relatime=on \
  -O utf8only=on \
  -O xattr=sa \
  -m none \
  -R /chroot \
  bpool $LAYOUT "${DISKS[@]/%/-part3}"

zpool create \
  -o ashift=12 \
  -o autotrim=off \
  -o compatibility=openzfs-2.1-linux \
  -O acltype=posixacl \
  -O atime=on \
  -O canmount=off \
  -O casesensitivity=sensitive \
  -O compression=zstd \
  -O dnodesize=auto \
  -O normalization=formD \
  -O overlay=off \
  -O relatime=on \
  -O utf8only=on \
  -O xattr=sa \
  -m none \
  -R /chroot \
  rpool $LAYOUT "${DISKS[@]/%/-part4}"

sync
sleep 2

zfs create -o canmount=off -o mountpoint=none rpool/ROOT
zfs create -o canmount=off -o mountpoint=none bpool/BOOT

zfs create -o mountpoint=/ rpool/ROOT/jammy

zfs create -o mountpoint=/boot bpool/BOOT/jammy

if [ "$LAYOUT" != "" ]; then
  zfs create bpool/grub
fi

# run this in a separate mount namespace to fix inability to export rpool ("pool is busy")
unshare --mount env DISKS="${DISKS[*]}" HOSTNAME="$HOSTNAME" PASSWORD="$PASSWORD" ARCH="$ARCH" ARCH_GRUB="$ARCH_GRUB" USERNAME="$USERNAME" SSH_KEY_PUB="$SSH_KEY_PUB" LAYOUT="$LAYOUT" bash </home/vagrant/namespace.sh

mount | grep -v zfs | tac | awk '/\/chroot/ {print $3}' |
  xargs -i{} umount -lf {}
zpool export -a
