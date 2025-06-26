#!/bin/bash
set -euxo pipefail

case $PACKER_BUILD_NAME in
ubuntu-box) ;;
ubuntu-lab)

  zpool create \
    -o ashift=12 \
    -o autotrim=on \
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
    dozer mirror /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_01000000000000000001 /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_02000000000000000001

  sync
  sleep 2

  sgdisk -p /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_01000000000000000001
  sgdisk -p /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_02000000000000000001

  disk=/dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_03000000000000000001
  sgdisk -n1:0:+1014M -t1:BF01 "$disk" # tank
  sgdisk -n2:0:-8M -t2:BF01 "$disk"    # mouse
  sgdisk -n3:0:0 -t2:BF07 "$disk"      # extra
  sgdisk -p "$disk"
  disk=/dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_04000000000000000001
  sgdisk -n1:0:+1014M -t1:BF01 "$disk" # tank
  sgdisk -n2:0:-8M -t2:BF01 "$disk"    # mouse
  sgdisk -n3:0:0 -t2:BF07 "$disk"      # extra
  sgdisk -p "$disk"

  sync
  sleep 2

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
    tank raidz2 /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_03000000000000000001-part1 /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_04000000000000000001-part1 /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_05000000000000000001 /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_06000000000000000001

  sync
  sleep 2

  sgdisk -p /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_05000000000000000001
  sgdisk -p /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_06000000000000000001

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
    mouse mirror /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_03000000000000000001-part2 /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_04000000000000000001-part2

  sync
  sleep 2

  ;;
ubuntu-pug)

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
    apoc mirror /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_01000000000000000001 /dev/disk/by-id/ata-VMware_Virtual_SATA_Hard_Drive_02000000000000000001

  sync
  sleep 2

  ;;
*)
  echo >&2 "Unknown build $PACKER_BUILD_NAME"
  exit 1
  ;;
esac
