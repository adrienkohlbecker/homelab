#!/usr/bin/env bash
# Seed and finalize the destructive qemu-only PRESERVE_META fixture.
set -euo pipefail

: "${DISKS:?}"
: "${EXTRA_DISKS:?}"
: "${PRESERVE_META:?}"
: "${PRESERVE_META_FIXTURE:?}"
: "${QEMU_TEST_IMAGE:?}"
: "${SOURCE_NAME:?}"
: "${UBUNTU_NAME:?}"

if [ "$PRESERVE_META" != true ] || [ "$PRESERVE_META_FIXTURE" != true ] ||
  [ "$QEMU_TEST_IMAGE" != true ] || [ "$SOURCE_NAME" != lab ]; then
  echo "preserve_meta_fixture.sh: restricted to the qemu lab PRESERVE_META fixture" >&2
  exit 1
fi

partdev() {
  local disk="$1" number="$2"
  case "$disk" in
  /dev/disk/by-id/*) echo "${disk}-part${number}" ;;
  /dev/nvme[0-9]*n[0-9]* | /dev/mmcblk[0-9]* | /dev/loop[0-9]* | /dev/md[0-9]*) echo "${disk}p${number}" ;;
  *) echo "${disk}${number}" ;;
  esac
}

wipe_fixture_disk() {
  local disk="$1"
  # Every fixture disk is a disposable qemu device; a missing old ZFS label is
  # expected on the first build and safe to ignore.
  zpool labelclear -f "$disk" || true
  wipefs -a "$disk"
  blkdiscard -f "$disk" || true
  sgdisk --zap-all "$disk"
}

export_pool() {
  local pool="$1"
  udevadm settle
  if ! zpool export "$pool"; then
    sleep 5
    zpool export "$pool"
  fi
}

FIXTURE_ZPOOL_OPTS=(
  -o ashift=12
  -o compatibility=openzfs-2.1-linux
  -O casesensitivity=insensitive
  -O normalization=formD
  -O utf8only=on
  -O acltype=posix
  -O atime=on
  -O canmount=off
  -O compression=zstd
  -O devices=off
  -O dnodesize=auto
  -O overlay=off
  -O relatime=on
  -O setuid=off
  -O xattr=sa
  -m none
)

create_md_fixture() {
  local name="$1" metadata="$2" level="$3" partition_number="$4"
  local disk
  local -a partitions=()

  for disk in $DISKS; do
    partitions+=("$(partdev "$disk" "$partition_number")")
  done
  mdadm --create "/dev/md/$name" --name="$name" --metadata="$metadata" \
    --level="$level" --bitmap=none --raid-devices="${#partitions[@]}" \
    "${partitions[@]}"
  udevadm settle
  mdadm --stop "/dev/md/$name"
  udevadm settle
  for disk in "${partitions[@]}"; do
    [ "$(blkid -s TYPE -o value "$disk")" = linux_raid_member ]
  done
}

seed() {
  local disk tm1 tm2 tank3 tank4
  local -a rpool_disks extra_disks meta_partitions legacy_rpool_partitions

  read -r -a rpool_disks <<<"$DISKS"
  read -r -a extra_disks <<<"$EXTRA_DISKS"
  [ "${#rpool_disks[@]}" -eq 3 ]
  [ "${#extra_disks[@]}" -eq 6 ]

  if zpool list -H -o name 2>/dev/null | grep -Eq '^(rpool|dozer|tank|mouse)$'; then
    echo "preserve_meta_fixture.sh: fixture pools must not already be imported" >&2
    exit 1
  fi

  meta_partitions=()
  legacy_rpool_partitions=()
  for disk in "${rpool_disks[@]}"; do
    wipe_fixture_disk "$disk"
    # QEMU exposes 512-byte logical sectors while prod's NVMes expose 4096.
    # These scaled bounds reproduce prod's exact 1013MiB:129GiB byte extent.
    sgdisk -a1 -n1:24K:+1000K -t1:EF02 -c1:bios "$disk"
    sgdisk -n2:1M:+1000M -t2:EF00 -c2:efi "$disk"
    sgdisk -a1 -n6:2074624:270510079 -t6:BF01 -c6:meta "$disk"
    sgdisk -n4:0:0 -t4:BF00 -c4:rpool "$disk"
    meta_partitions+=("$(partdev "$disk" 6)")
    legacy_rpool_partitions+=("$(partdev "$disk" 4)")
  done

  for disk in "${extra_disks[@]}"; do
    wipe_fixture_disk "$disk"
  done
  udevadm settle

  zpool create -f -o autotrim=on "${FIXTURE_ZPOOL_OPTS[@]}" dozer mirror \
    "${extra_disks[0]}" "${extra_disks[1]}"

  tm1=${extra_disks[2]}
  tm2=${extra_disks[3]}
  tank3=${extra_disks[4]}
  tank4=${extra_disks[5]}
  for disk in "$tm1" "$tm2"; do
    sgdisk -n1:0:+1014M -t1:BF01 "$disk"
    sgdisk -n2:0:-8M -t2:BF01 "$disk"
    sgdisk -n3:0:0 -t3:BF07 "$disk"
  done
  udevadm settle

  zpool create -f -o autotrim=on "${FIXTURE_ZPOOL_OPTS[@]}" \
    tank raidz2 "$(partdev "$tm1" 1)" "$(partdev "$tm2" 1)" "$tank3" "$tank4" \
    special mirror "${meta_partitions[@]}"
  zpool create -f -o autotrim=off "${FIXTURE_ZPOOL_OPTS[@]}" \
    mouse mirror "$(partdev "$tm1" 2)" "$(partdev "$tm2" 2)"

  # Match the live Jammy handoff: p2 carries stopped md-EFI metadata and p4
  # carries the exported legacy rpool. The first preservation pass must remove
  # both signatures without touching the p6 tank labels.
  zpool create -f -o ashift=12 -o compatibility=openzfs-2.1-linux \
    -O mountpoint=none rpool mirror "${legacy_rpool_partitions[@]}"
  create_md_fixture preserve_meta_efi 1.0 raid1 2

  export_pool rpool
  export_pool mouse
  export_pool tank
  export_pool dozer
}

prepare_retry() {
  local disk
  local -a retry_rpool_partitions=()

  # Match the stopped state required by the runbook after a failed new-layout
  # install: p2-p4 retain md member signatures and p5 retains exported rpool
  # labels. The second production partitioning pass must clear all four kinds.
  create_md_fixture preserve_meta_efi 1.0 raid1 2
  create_md_fixture preserve_meta_swap 1.2 raid1 3
  create_md_fixture preserve_meta_podman 1.2 raid5 4
  for disk in $DISKS; do
    retry_rpool_partitions+=("$(partdev "$disk" 5)")
  done
  zpool create -f -o ashift=12 -o compatibility=openzfs-2.1-linux \
    -O mountpoint=none rpool mirror "${retry_rpool_partitions[@]}"
  export_pool rpool

  for disk in $DISKS; do
    zdb -l "$(partdev "$disk" 5)" | grep -Fq "name: 'rpool'"
  done
}

finalize() {
  # provision.sh has completed and exported rpool without ever importing tank.
  # Re-import the auxiliary fixture pools only now so the shipped cache covers
  # dozer, tank, and mouse. ZFSBootMenu imports rpool independently at boot.
  zpool import -N -R /mnt -o cachefile=none rpool
  zfs mount "rpool/ROOT/$UBUNTU_NAME"
  for pool in dozer tank mouse; do
    zpool import -N -d /dev -o cachefile=/etc/zfs/zpool.cache "$pool"
  done
  install -d /mnt/etc/zfs
  cp /etc/zfs/zpool.cache /mnt/etc/zfs/zpool.cache
  zfs unmount "rpool/ROOT/$UBUNTU_NAME"

  export_pool mouse
  export_pool tank
  export_pool dozer
  export_pool rpool
}

case ${1:-} in
seed) seed ;;
prepare_retry) prepare_retry ;;
finalize) finalize ;;
*)
  echo "usage: preserve_meta_fixture.sh {seed|prepare_retry|finalize}" >&2
  exit 2
  ;;
esac
