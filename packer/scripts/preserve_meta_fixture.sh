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
  zpool export "$pool"
}

seed() {
  local disk tm1 tm2 tank3 tank4
  local -a rpool_disks extra_disks meta_partitions

  read -r -a rpool_disks <<<"$DISKS"
  read -r -a extra_disks <<<"$EXTRA_DISKS"
  [ "${#rpool_disks[@]}" -eq 3 ]
  [ "${#extra_disks[@]}" -eq 6 ]

  if zpool list -H -o name 2>/dev/null | grep -Eq '^(rpool|dozer|tank|mouse)$'; then
    echo "preserve_meta_fixture.sh: fixture pools must not already be imported" >&2
    exit 1
  fi

  meta_partitions=()
  for disk in "${rpool_disks[@]}"; do
    wipe_fixture_disk "$disk"
    # QEMU exposes 512-byte logical sectors while prod's NVMes expose 4096.
    # These scaled bounds reproduce prod's exact 1013MiB:129GiB byte extent.
    sgdisk -a1 -n6:2074624:270510079 -t6:BF01 -c6:meta "$disk"
    meta_partitions+=("$(partdev "$disk" 6)")
  done

  for disk in "${extra_disks[@]}"; do
    wipe_fixture_disk "$disk"
  done
  udevadm settle

  zpool create -f -o ashift=12 -o autotrim=on \
    -o compatibility=openzfs-2.1-linux \
    -O compression=zstd -O mountpoint=none dozer mirror \
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

  zpool create -f -o ashift=12 -o autotrim=on \
    -o compatibility=openzfs-2.1-linux \
    -O compression=zstd -O mountpoint=none \
    tank raidz2 "$(partdev "$tm1" 1)" "$(partdev "$tm2" 1)" "$tank3" "$tank4" \
    special mirror "${meta_partitions[@]}"
  zpool create -f -o ashift=12 -o autotrim=off \
    -o compatibility=openzfs-2.1-linux \
    -O compression=zstd -O mountpoint=none \
    mouse mirror "$(partdev "$tm1" 2)" "$(partdev "$tm2" 2)"

  export_pool mouse
  export_pool tank
  export_pool dozer
}

finalize() {
  # provision.sh has completed and exported rpool without ever importing tank.
  # Re-import the fixture pools only now so the shipped test image's cache
  # retains the same pool surface as the ordinary lab artifact.
  zpool import -N -R /mnt -o cachefile=/etc/zfs/zpool.cache rpool
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
finalize) finalize ;;
*)
  echo "usage: preserve_meta_fixture.sh {seed|finalize}" >&2
  exit 2
  ;;
esac
