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

for disk in "${DISKS[@]}"; do
  zpool labelclear -f "$disk" || true
  wipefs -a "$disk"
  blkdiscard -f "$disk" || true
  sgdisk --zap-all "$disk"

  sync
  sleep 2

  sgdisk -n1:1M:+512M -t1:EF00 "$disk"       # EFI (EF00 = EFI system partition)
  sgdisk -a1 -n4:24K:+1000K -t4:EF02 "$disk" # MBR booting (EF02 = BIOS boot partition)

  if [ "$LAYOUT" = "" ]; then
    sgdisk -n2:0:+500M -t2:8200 "$disk" # Swap (8200 = Linux Swap)
  else
    sgdisk -n2:0:+500M -t2:FD00 "$disk" # Swap (FD00 = Linux RAID)
  fi

  sgdisk -p "$disk"
  if [ "$PACKER_BUILD_NAME" = "ubuntu-lab" ]; then
    sgdisk -n5:-2G:0 -t5:BF01 "$disk" # metadata vdev (BF01 = Solaris /usr & Mac ZFS, default when doing zpool create)
  fi
  sgdisk -n3:0:0 -t3:BF00 "$disk" # rpool (BF00 = Solaris root)

  sgdisk -p "$disk"

  sync
  sleep 2
done

mkdir -p /chroot
zgenhostid -f 0x00bab10c

# Create the zpool
zpool create \
  -o ashift=12 \
  -o autotrim=on \
  -o compatibility=openzfs-2.1-linux \
  -O acltype=posixacl \
  -O atime=on \
  -O canmount=off \
  -O casesensitivity=sensitive \
  -O compression=lz4 \
  -O dnodesize=auto \
  -O normalization=formD \
  -O overlay=off \
  -O relatime=on \
  -O utf8only=on \
  -O xattr=sa \
  -m none \
  rpool $LAYOUT "${DISKS[@]/%/-part3}"

sync
sleep 2

# Create initial file systems
zfs create -o canmount=off -o mountpoint=none rpool/ROOT
zfs create -o canmount=noauto -o mountpoint=/ rpool/ROOT/noble
zpool set bootfs=rpool/ROOT/noble rpool

zpool export rpool
zpool import -N -R /chroot rpool
zfs mount rpool/ROOT/noble

# Update device symlinks
udevadm trigger

# run this in a separate mount namespace to fix inability to export rpool ("pool is busy")
unshare --mount env DISKS="${DISKS[*]}" HOSTNAME="$HOSTNAME" PASSWORD="$PASSWORD" ARCH="$ARCH" ARCH_GRUB="$ARCH_GRUB" USERNAME="$USERNAME" SSH_KEY_PUB="$SSH_KEY_PUB" LAYOUT="$LAYOUT" bash </home/vagrant/namespace.sh

mount | grep -v zfs | tac | awk '/\/chroot/ {print $3}' |
  xargs -i{} umount -lf {}
zpool export -a
