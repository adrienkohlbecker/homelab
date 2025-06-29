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
    dozer mirror /dev/vde /dev/vdf

  sync
  sleep 2

  sgdisk -p /dev/vde
  sgdisk -p /dev/vdf

  disk=/dev/vdg
  sgdisk -n1:0:+1014M -t1:BF01 "$disk" # tank
  sgdisk -n2:0:-8M -t2:BF01 "$disk"    # mouse
  sgdisk -n3:0:0 -t2:BF07 "$disk"      # extra
  sgdisk -p "$disk"
  disk=/dev/vdh
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
    tank raidz2 /dev/vdg1 /dev/vdh1 /dev/vdi /dev/vdj

  sync
  sleep 2

  sgdisk -p /dev/vdi
  sgdisk -p /dev/vdj

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
    mouse mirror /dev/vdg2 /dev/vdh2

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
    apoc mirror /dev/vdc /dev/vdd

  sync
  sleep 2

  ;;
*)
  echo >&2 "Unknown build $PACKER_BUILD_NAME"
  exit 1
  ;;
esac
