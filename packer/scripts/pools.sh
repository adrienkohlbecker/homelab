#!/bin/bash
# Create the non-rpool ZFS pools that prod-shape host fixtures need.
# Invoked from provision.sh between the rpool arch-chroot and the
# final rpool export, while /mnt is still rpool's mounted root --
# so the cachefile copy at the end of this script lands in the
# shipped install's /etc/zfs/zpool.cache and zfs-import-cache.service
# auto-imports the pools on first boot.
#
# Inputs (env):
#   EXTRA_DISKS  -- space-delimited disk paths to consume here. Order
#                   matters: each pool pops disks off the front in the
#                   sequence EXTRA_POOLS dictates.
#   EXTRA_POOLS  -- space-delimited list of pool layouts to create.
#                   Layouts:
#                     apoc        -- 2 disks, mirror. (pug prod shape)
#                     dozer       -- 2 disks, mirror. (lab prod shape)
#                     tank_mouse  -- 4 disks. First two get partitioned
#                                    (tank/mouse/extra); tank is raidz2
#                                    over the p1's of those two plus the
#                                    whole disks 3-4. mouse is a mirror
#                                    over the p2's of disks 1-2. Matches
#                                    the lab prod host's tank+mouse
#                                    layout.
#
# Idempotence: each `zpool create` is gated on a list check so bare-
# metal copy-paste reruns skip existing pools. The build VM hits the
# pools fresh, so the gate is just defensive there.
set -euxo pipefail

if [ -z "${EXTRA_POOLS:-}" ]; then
  exit 0
fi

read -r -a _disks <<<"${EXTRA_DISKS:-}"

pop_disks() {
  local n=$1 i out=""
  for ((i = 0; i < n; i++)); do
    if [ "${#_disks[@]}" -eq 0 ]; then
      echo >&2 "pools.sh: ran out of EXTRA_DISKS while allocating $n for $POOL"
      exit 1
    fi
    out+="${_disks[0]} "
    _disks=("${_disks[@]:1}")
  done
  echo "${out% }"
}

wipe_disk() {
  zpool labelclear -f "$1" || true
  wipefs -a "$1"
  blkdiscard -f "$1" || true
  sgdisk --zap-all "$1"
}

# Defaults applied to every pool. -O exec= intentionally omitted
# (default exec=on) because rootless-container bind-mounts inherit
# the source mount's noexec flag, which surfaces as EACCES on execve
# from the checkout -- precedent is github_runner's actions/runner
# --work folder living on dozer/scratch/github_runner/. setuid=off +
# devices=off stay for hardening.
ZPOOL_OPTS=(
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

create_apoc() {
  local disks
  disks=$(pop_disks 2)
  if zpool list -H apoc >/dev/null 2>&1; then return; fi
  for d in $disks; do wipe_disk "$d"; done
  udevadm settle
  # shellcheck disable=SC2086
  zpool create -f -o autotrim=off "${ZPOOL_OPTS[@]}" apoc mirror $disks
}

create_dozer() {
  local disks
  disks=$(pop_disks 2)
  if zpool list -H dozer >/dev/null 2>&1; then return; fi
  for d in $disks; do wipe_disk "$d"; done
  udevadm settle
  # shellcheck disable=SC2086
  zpool create -f -o autotrim=on "${ZPOOL_OPTS[@]}" dozer mirror $disks
}

create_tank_mouse() {
  local disks tm1 tm2 tank3 tank4
  disks=$(pop_disks 4)
  # shellcheck disable=SC2086
  set -- $disks
  tm1=$1
  tm2=$2
  tank3=$3
  tank4=$4
  for d in $disks; do wipe_disk "$d"; done
  # Partition the shared (tank+mouse) disks: p1 = tank slice (1014M),
  # p2 = mouse slice (fill minus 8M), p3 = future-use extra (last 8M).
  for tm in "$tm1" "$tm2"; do
    sgdisk -n1:0:+1014M -t1:BF01 "$tm"
    sgdisk -n2:0:-8M -t2:BF01 "$tm"
    sgdisk -n3:0:0 -t3:BF07 "$tm"
    sgdisk -p "$tm"
  done
  udevadm settle
  if ! zpool list -H tank >/dev/null 2>&1; then
    zpool create -f -o autotrim=off "${ZPOOL_OPTS[@]}" \
      tank raidz2 "${tm1}1" "${tm2}1" "$tank3" "$tank4"
  fi
  if ! zpool list -H mouse >/dev/null 2>&1; then
    zpool create -f -o autotrim=off "${ZPOOL_OPTS[@]}" \
      mouse mirror "${tm1}2" "${tm2}2"
  fi
}

for POOL in $EXTRA_POOLS; do
  case "$POOL" in
  apoc) create_apoc ;;
  dozer) create_dozer ;;
  tank_mouse) create_tank_mouse ;;
  *)
    echo >&2 "pools.sh: unknown EXTRA_POOLS entry '$POOL'"
    exit 1
    ;;
  esac
done

# Sync the cachefile into the shipped install so zfs-import-cache.service
# auto-imports these pools on first boot. /mnt is still rpool's root at
# this point (provision.sh hasn't unmounted yet). Without this copy the
# pools stay on-disk but unmounted on the test VM.
mkdir -p /mnt/etc/zfs
cp /etc/zfs/zpool.cache /mnt/etc/zfs/zpool.cache
