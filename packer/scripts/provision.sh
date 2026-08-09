#!/bin/bash
# Bootstrap a ZFS-on-root install onto $DISKS. Used by packer's qemu
# build and as the bare-metal copy-paste path for provisioning new
# lab-class hosts.
#
# Bare-metal callers MUST also:
#  - pre-flight $DISKS. Unless PRESERVE_META=true, every entry is wiped
#    unconditionally (sgdisk --zap-all + wipefs + blkdiscard + zpool
#    labelclear); a wrong device path destroys data in seconds. The preserve
#    path is intentionally lab-specific and validates every p6 before changing
#    any partition table.
#  - rotate /home/vagrant/.ssh/authorized_keys (which currently holds
#    the publicly-known vagrant insecure pubkey) and remove
#    /etc/sudoers.d/vagrant before the host gets a routable IP. The
#    shipped image is otherwise a free root shell on any lab LAN.
#    Moot when TARGET_USERNAME + SSH_KEY_PUB are set: the real operator
#    user is created instead and no vagrant user ever exists.
#  - on mirror-rpool variants, supply matching-size disks. The
#    rpool mirror caps at the smallest disk's partition 5, so a
#    2T+4T+4T mix silently halves usable rpool capacity.
#  - verify the rpool ashift=12 below matches the disks. 4 KiB is
#    right for ~95% of drives but some enterprise SSDs / SMR HDDs
#    report 8 KiB / 16 KiB physical (ashift=13 / 14). ashift can't
#    be changed after pool creation; getting it wrong loses perf.
#  - sync the host clock (chronyd -q / ntpdate / similar) before
#    invoking the script. RTC at 1970 or factory default trips TLS
#    cert verification on the gitlab.com ZBM tarball pull.
#  - disable secure boot in firmware setup. rEFInd's EFI binary is
#    signed by the rEFInd project, not Microsoft, so secure-boot-
#    enforcing OEM firmware (locked-down Lenovo / Dell / etc.) will
#    refuse to load it.
set -euxo pipefail

# Exact marker checked by the lab rebuild runbook before the live installer is
# allowed to touch the three rpool disks.
# shellcheck disable=SC2034  # consumed by the runbook's source-integrity gate
PRESERVE_META_CONTRACT=1

# DISKS, EXTRA_DISKS, LAYOUT, SWAP_SIZE, PODMAN_SIZE, META_SIZE, EXTRA_POOLS,
# PRESERVE_META, PRESERVE_META_FIXTURE, SOURCE_NAME, IMAGE_TARGET,
# QEMU_TEST_IMAGE, UBUNTU_NAME, and UBUNTU_MIRROR come from packer's
# shell-provisioner env block. Bare-metal callers export them by hand.
# This script consumes the disk/pool vars and passes the exported install vars
# through to chroot.sh. The ZBM_*/REFIND_*/UBUNTU_MIRROR_* vars used downstream
# are documented at the top of chroot.sh.

PRESERVE_META=${PRESERVE_META:-false}
PRESERVE_META_FIXTURE=${PRESERVE_META_FIXTURE:-false}

preflight() {
  local name disk disks_count extra_disks_count
  local -A seen_disks=()
  local required=(
    DISKS EXTRA_DISKS LAYOUT SWAP_SIZE PODMAN_SIZE META_SIZE EXTRA_POOLS
    UBUNTU_NAME UBUNTU_MIRROR UBUNTU_MIRROR_SECURITY
    UBUNTU_MIRROR_UPSTREAM UBUNTU_MIRROR_SECURITY_UPSTREAM
  )

  for name in "${required[@]}"; do
    if ! [[ -v $name ]]; then
      echo "provision.sh: required variable $name is not set" >&2
      return 1
    fi
  done

  for name in DISKS SWAP_SIZE UBUNTU_NAME UBUNTU_MIRROR UBUNTU_MIRROR_SECURITY UBUNTU_MIRROR_UPSTREAM UBUNTU_MIRROR_SECURITY_UPSTREAM; do
    if [ -z "${!name}" ]; then
      echo "provision.sh: required variable $name is empty" >&2
      return 1
    fi
  done

  case $LAYOUT in '' | mirror) ;; *)
    echo "provision.sh: LAYOUT must be empty or mirror (got '$LAYOUT')" >&2
    return 1
    ;;
  esac
  case ${IMAGE_TARGET:-qemu} in qemu | hetzner) ;; *)
    echo "provision.sh: IMAGE_TARGET must be qemu or hetzner (got '$IMAGE_TARGET')" >&2
    return 1
    ;;
  esac
  case ${QEMU_TEST_IMAGE:-false} in true | false) ;; *)
    echo "provision.sh: QEMU_TEST_IMAGE must be true or false (got '$QEMU_TEST_IMAGE')" >&2
    return 1
    ;;
  esac
  case $PRESERVE_META in true | false) ;; *)
    echo "provision.sh: PRESERVE_META must be true or false (got '$PRESERVE_META')" >&2
    return 1
    ;;
  esac
  case $PRESERVE_META_FIXTURE in true | false) ;; *)
    echo "provision.sh: PRESERVE_META_FIXTURE must be true or false (got '$PRESERVE_META_FIXTURE')" >&2
    return 1
    ;;
  esac

  if { [[ -v TARGET_HOSTNAME ]] && ! [[ -v TARGET_USERNAME ]]; } ||
    { [[ -v TARGET_USERNAME ]] && ! [[ -v TARGET_HOSTNAME ]]; }; then
    echo "provision.sh: TARGET_HOSTNAME and TARGET_USERNAME must be set together" >&2
    return 1
  fi
  if [[ -v TARGET_HOSTNAME ]] && ! [[ $TARGET_HOSTNAME =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
    echo "provision.sh: invalid TARGET_HOSTNAME '$TARGET_HOSTNAME'" >&2
    return 1
  fi
  if [[ -v TARGET_USERNAME ]] && ! [[ $TARGET_USERNAME =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
    echo "provision.sh: invalid TARGET_USERNAME '$TARGET_USERNAME'" >&2
    return 1
  fi

  if [ "${IMAGE_TARGET:-qemu}" != hetzner ]; then
    if ! [[ -v SSH_KEY_PUB ]] || ! [[ $SSH_KEY_PUB =~ ^(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521))[[:space:]] ]]; then
      echo "provision.sh: SSH_KEY_PUB must contain a supported public key" >&2
      return 1
    fi
  fi

  for disk in $DISKS $EXTRA_DISKS; do
    if [[ $disk != /dev/* ]]; then
      echo "provision.sh: disk path must start with /dev/ (got '$disk')" >&2
      return 1
    fi
    if [[ -v seen_disks[$disk] ]]; then
      echo "provision.sh: disk '$disk' is listed more than once" >&2
      return 1
    fi
    seen_disks[$disk]=1
  done

  if [ "$PRESERVE_META" = true ]; then
    disks_count=$(wc -w <<<"$DISKS")
    extra_disks_count=$(wc -w <<<"$EXTRA_DISKS")
    if [ "$LAYOUT" != mirror ] || [ "$disks_count" -ne 3 ]; then
      echo "provision.sh: PRESERVE_META requires LAYOUT=mirror and exactly three DISKS" >&2
      return 1
    fi
    if [ "$META_SIZE" != 128G ] || [ -z "$PODMAN_SIZE" ]; then
      echo "provision.sh: PRESERVE_META requires META_SIZE=128G and a non-empty PODMAN_SIZE" >&2
      return 1
    fi
    if [ -n "$EXTRA_POOLS" ]; then
      echo "provision.sh: PRESERVE_META forbids EXTRA_POOLS; existing pools must stay untouched" >&2
      return 1
    fi
    if [ "$PRESERVE_META_FIXTURE" = true ]; then
      if [ "${QEMU_TEST_IMAGE:-false}" != true ] || [ "${SOURCE_NAME:-}" != lab ] || [ "$extra_disks_count" -ne 6 ]; then
        echo "provision.sh: PRESERVE_META_FIXTURE is restricted to the six-disk qemu lab fixture" >&2
        return 1
      fi
    elif [ "$extra_disks_count" -ne 0 ]; then
      echo "provision.sh: production PRESERVE_META requires EXTRA_DISKS to be empty" >&2
      return 1
    fi
  elif [ "$PRESERVE_META_FIXTURE" = true ]; then
    echo "provision.sh: PRESERVE_META_FIXTURE requires PRESERVE_META=true" >&2
    return 1
  fi
}

preflight

export DISKS_COUNT
DISKS_COUNT=$(wc -w <<<"$DISKS")

# Placeholder hostname for the shipped image — the deploy step
# (ansible / cloud-init / bare-metal wrapper) is expected to overwrite
# it before first boot. USERNAME is the vagrant user chroot.sh creates
# so packer can SSH back in for the next provisioner stage. Bare-metal
# callers override both via TARGET_HOSTNAME / TARGET_USERNAME (distinct
# names: the live-USB shell already sets HOSTNAME) so the installed
# system boots as the real host with the real operator user and no
# vagrant user is ever created.
export HOSTNAME="${TARGET_HOSTNAME:-ubuntu}"
export USERNAME="${TARGET_USERNAME:-vagrant}"

# Directory holding chroot.sh. The qemu build VM's packer file-provisioner
# lands it in /home/vagrant.
SCRIPTS_DIR="${SCRIPTS_DIR:-/home/vagrant}"

# Map (disk, partition number) to the kernel/udev partition device.
# vd*/sd*/hd* tack the digit on directly; nvme/mmcblk/loop/md need a
# 'p' separator; /dev/disk/by-id symlinks use '-partN'. Passing
# /dev/nvme0n1 through ${DISKS[@]/%/3} would yield /dev/nvme0n13 --
# real bug if the script is ever pointed at non-virtio disks.
partdev() {
  local disk="$1" n="$2"
  case "$disk" in
  /dev/disk/by-id/*) echo "${disk}-part${n}" ;;
  /dev/nvme[0-9]*n[0-9]* | /dev/mmcblk[0-9]* | /dev/loop[0-9]* | /dev/md[0-9]*) echo "${disk}p${n}" ;;
  *) echo "${disk}${n}" ;;
  esac
}

# Export a pool, tolerating the transient "pool is busy" race where
# udev/systemd still hold a handle on a freshly-created dataset or zvol
# device node (matches upstream openzfs/zfs#16036) — the freshly-created
# rpool datasets whose udev probe can trip the very next `zpool export`.
# `zpool export -f` doesn't bypass the spa_refcount EBUSY gate, so force
# is pointless. udevadm settle drains
# pending uevents; one retry after 5s covers the rare slow-drain case.
# A second failure is a genuinely wedged pool — let the build fail
# rather than ship an image that wasn't cleanly quiesced.
zpool_export_retry() {
  local pool="$1"
  udevadm settle
  if ! zpool export "$pool"; then
    sleep 5
    zpool export "$pool"
  fi
}

wipe_disk() {
  zpool labelclear -f "$1" || true
  wipefs -a "$1"
  blkdiscard -f "$1" || true
  sgdisk --zap-all "$1"
}

PRESERVE_META_STATE_DIR=""
if [ "$PRESERVE_META_FIXTURE" = true ]; then
  # QEMU virtio exposes 512-byte logical sectors. Scale the live NVMe sector
  # numbers by eight so the fixture covers the identical 1013MiB start,
  # 128GiB payload, and 129GiB end.
  PRESERVE_META_SECTOR_SIZE=512
  PRESERVE_META_FIRST_SECTOR=2074624
  PRESERVE_META_LAST_SECTOR=270510079
  PRESERVE_META_SECTOR_COUNT=268435456
else
  PRESERVE_META_SECTOR_SIZE=4096
  PRESERVE_META_FIRST_SECTOR=259328
  PRESERVE_META_LAST_SECTOR=33813759
  PRESERVE_META_SECTOR_COUNT=33554432
fi

capture_preserved_meta() {
  local disk index part info zdb_out partition_guid pool_guid top_guid leaf_guid number
  local expected_pool_guid="" expected_top_guid=""
  local -A leaf_guids=()

  if zpool list -H -o name 2>/dev/null | grep -Eq '^(rpool|tank)$'; then
    echo "provision.sh: rpool and tank must both be exported before PRESERVE_META" >&2
    return 1
  fi

  PRESERVE_META_STATE_DIR=$(mktemp -d /tmp/preserve_meta.XXXXXX)
  index=0
  for disk in $DISKS; do
    index=$((index + 1))
    part=$(partdev "$disk" 6)
    info="$PRESERVE_META_STATE_DIR/$index.sgdisk"
    zdb_out="$PRESERVE_META_STATE_DIR/$index.zdb"

    if [ ! -b "$disk" ] || [ ! -b "$part" ]; then
      echo "provision.sh: expected whole disk '$disk' and preserved partition '$part'" >&2
      return 1
    fi
    if [ "$(blockdev --getss "$disk")" -ne "$PRESERVE_META_SECTOR_SIZE" ]; then
      echo "provision.sh: '$disk' has the wrong logical sector size for PRESERVE_META" >&2
      return 1
    fi

    number=0
    while read -r number; do
      if [ "$number" -gt 6 ]; then
        echo "provision.sh: unexpected partition $number on '$disk'; only p1-p6 are allowed" >&2
        return 1
      fi
    done < <(sgdisk -p "$disk" | awk '$1 ~ /^[0-9]+$/ { print $1 }')
    if [ "$(sgdisk -p "$disk" | awk '$1 == 6 { count++ } END { print count + 0 }')" -ne 1 ]; then
      echo "provision.sh: '$disk' must contain exactly one p6" >&2
      return 1
    fi

    sgdisk -i 6 "$disk" >"$info"
    grep -Fqx 'Partition GUID code: 6A898CC3-1DD2-11B2-99A6-080020736631 (Solaris /usr & Mac ZFS)' "$info"
    grep -Fqx "First sector: $PRESERVE_META_FIRST_SECTOR (at 1013.0 MiB)" "$info"
    grep -Fqx "Last sector: $PRESERVE_META_LAST_SECTOR (at 129.0 GiB)" "$info"
    grep -Fqx "Partition size: $PRESERVE_META_SECTOR_COUNT sectors (128.0 GiB)" "$info"
    grep -Fqx "Partition name: 'meta'" "$info"

    partition_guid=$(awk -F': ' '/^Partition unique GUID:/ { print $2 }' "$info")
    [[ $partition_guid =~ ^[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}$ ]] || {
      echo "provision.sh: invalid p6 partition GUID on '$disk': '$partition_guid'" >&2
      return 1
    }
    printf '%s\n' "$partition_guid" >"$PRESERVE_META_STATE_DIR/$index.partition_guid"

    zdb -l "$part" >"$zdb_out"
    grep -Fq "name: 'tank'" "$zdb_out"
    grep -Fq 'vdev_children: 2' "$zdb_out"
    grep -Fq "type: 'mirror'" "$zdb_out"
    grep -Fq 'children[2]:' "$zdb_out"
    if grep -Fq 'children[3]:' "$zdb_out"; then
      echo "provision.sh: tank p6 labels describe more than three mirror children" >&2
      return 1
    fi
    if [ "$(grep -c '^[[:space:]]*path:' "$zdb_out")" -ne 3 ]; then
      echo "provision.sh: tank p6 labels do not describe exactly three mirror leaves" >&2
      return 1
    fi

    pool_guid=$(awk '$1 == "pool_guid:" { print $2; exit }' "$zdb_out")
    top_guid=$(awk '$1 == "top_guid:" { print $2; exit }' "$zdb_out")
    leaf_guid=$(awk '$1 == "guid:" { print $2; exit }' "$zdb_out")
    if [ -z "$pool_guid" ] || [ -z "$top_guid" ] || [ -z "$leaf_guid" ]; then
      echo "provision.sh: incomplete tank ZFS label identity on '$part'" >&2
      return 1
    fi
    if [ -n "$expected_pool_guid" ] && { [ "$pool_guid" != "$expected_pool_guid" ] || [ "$top_guid" != "$expected_top_guid" ]; }; then
      echo "provision.sh: p6 devices do not belong to one tank special mirror" >&2
      return 1
    fi
    if [[ -v leaf_guids[$leaf_guid] ]]; then
      echo "provision.sh: duplicate tank leaf GUID '$leaf_guid'" >&2
      return 1
    fi
    expected_pool_guid=$pool_guid
    expected_top_guid=$top_guid
    leaf_guids[$leaf_guid]=1
  done
}

verify_preserved_meta() {
  local disk index part info zdb_out partition_guid

  index=0
  for disk in $DISKS; do
    index=$((index + 1))
    part=$(partdev "$disk" 6)
    info="$PRESERVE_META_STATE_DIR/$index.verify.sgdisk"
    zdb_out="$PRESERVE_META_STATE_DIR/$index.verify.zdb"

    [ -b "$part" ]
    sgdisk -i 6 "$disk" >"$info"
    partition_guid=$(awk -F': ' '/^Partition unique GUID:/ { print $2 }' "$info")
    [ "$partition_guid" = "$(<"$PRESERVE_META_STATE_DIR/$index.partition_guid")" ]
    cmp "$PRESERVE_META_STATE_DIR/$index.sgdisk" "$info"
    zdb -l "$part" >"$zdb_out"
    cmp "$PRESERVE_META_STATE_DIR/$index.zdb" "$zdb_out"
  done
}

partition_disk_preserving_meta() {
  local disk="$1" number part

  for number in 1 2 3 4 5; do
    part=$(partdev "$disk" "$number")
    [ -b "$part" ] || continue
    if [ "$number" -eq 4 ] || [ "$number" -eq 5 ]; then
      # Initial p4 and retry-created p5 may carry rpool labels; absence is the
      # expected benign case for the other layout position.
      zpool labelclear -f "$part" || true
    fi
    wipefs -a "$part"
    sgdisk --delete="$number" "$disk"
  done

  sgdisk -a1 -n1:24K:+1000K -t1:EF02 -c1:bios "$disk"
  sgdisk -n2:1M:+1000M -t2:EF00 -c2:efi "$disk"
  sgdisk "-n3:0:+$SWAP_SIZE" -t3:8200 -c3:swap "$disk"
  sgdisk "-n4:0:+$PODMAN_SIZE" -t4:8300 -c4:podman "$disk"
  sgdisk -n5:0:0 -t5:BF00 -c5:rpool "$disk"
  sgdisk -p "$disk"
}

partition_disks_preserving_meta() {
  local disk

  # A stopped md array can be incrementally reassembled by udev when an earlier
  # disk's partition table changes, making a later member busy mid-cleanup.
  # Freeze rule execution across all three tables, stop any lab md arrays, then
  # publish the completed layouts to the kernel as one unit.
  udevadm control --stop-exec-queue
  trap 'udevadm control --start-exec-queue' EXIT
  mdadm --stop --scan || true
  for disk in $DISKS; do
    partition_disk_preserving_meta "$disk"
  done
  udevadm control --start-exec-queue
  trap - EXIT
  for disk in $DISKS; do
    partprobe "$disk"
  done
  udevadm settle
}

EXTRA_ZPOOL_OPTS=(
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

pop_extra_disks() {
  local n=$1 pool=$2 i
  POPPED_EXTRA_DISKS=()
  for ((i = 0; i < n; i++)); do
    if [ "${#EXTRA_DISK_QUEUE[@]}" -eq 0 ]; then
      echo >&2 "provision.sh: ran out of EXTRA_DISKS while allocating $n for $pool"
      exit 1
    fi
    POPPED_EXTRA_DISKS+=("${EXTRA_DISK_QUEUE[0]}")
    EXTRA_DISK_QUEUE=("${EXTRA_DISK_QUEUE[@]:1}")
  done
}

create_extra_apoc() {
  pop_extra_disks 2 apoc
  local extra_pool_disks=("${POPPED_EXTRA_DISKS[@]}")
  if zpool list -H apoc >/dev/null 2>&1; then return; fi
  for d in "${extra_pool_disks[@]}"; do wipe_disk "$d"; done
  udevadm settle
  zpool create -f -o autotrim=off "${EXTRA_ZPOOL_OPTS[@]}" apoc mirror "${extra_pool_disks[@]}"
}

create_extra_dozer() {
  pop_extra_disks 2 dozer
  local extra_pool_disks=("${POPPED_EXTRA_DISKS[@]}")
  if zpool list -H dozer >/dev/null 2>&1; then return; fi
  for d in "${extra_pool_disks[@]}"; do wipe_disk "$d"; done
  udevadm settle
  zpool create -f -o autotrim=on "${EXTRA_ZPOOL_OPTS[@]}" dozer mirror "${extra_pool_disks[@]}"
}

create_extra_zee() {
  pop_extra_disks 1 zee
  local disk="${POPPED_EXTRA_DISKS[0]}"
  if zpool list -H zee >/dev/null 2>&1; then return; fi
  wipe_disk "$disk"
  udevadm settle
  zpool create -f -o autotrim=on "${EXTRA_ZPOOL_OPTS[@]}" zee "$disk"
  zfs create -o canmount=on -o mountpoint=/zee/data zee/data
}

create_extra_tank_mouse() {
  pop_extra_disks 4 tank_mouse
  local extra_pool_disks=("${POPPED_EXTRA_DISKS[@]}")
  local tm1=${POPPED_EXTRA_DISKS[0]} tm2=${POPPED_EXTRA_DISKS[1]} tank3=${POPPED_EXTRA_DISKS[2]} tank4=${POPPED_EXTRA_DISKS[3]}
  for d in "${extra_pool_disks[@]}"; do wipe_disk "$d"; done
  for tm in "$tm1" "$tm2"; do
    sgdisk -n1:0:+1014M -t1:BF01 "$tm"
    sgdisk -n2:0:-8M -t2:BF01 "$tm"
    sgdisk -n3:0:0 -t3:BF07 "$tm"
    sgdisk -p "$tm"
  done
  udevadm settle
  if ! zpool list -H tank >/dev/null 2>&1; then
    # tank's special vdev lives on the fast NVMe rpool-mirror disks (their p6
    # meta partitions, $PARTITIONS_META), not on tank's own slow raidz2 HDDs --
    # so tank metadata + small-block datasets (special_small_blocks, set per
    # dataset by the zfs role) land on NVMe. A mirror across all the meta
    # partitions tolerates the same disk loss as the raidz2 (losing the special
    # vdev loses the pool). Empty on single-disk hosts (no meta partition), so
    # tank then has no special vdev. See notes/special-vdev-sizing.md.
    local special_args=()
    if [ -n "${PARTITIONS_META:-}" ]; then
      # shellcheck disable=SC2206  # word-split PARTITIONS_META into vdev members
      special_args=(special mirror $PARTITIONS_META)
    fi
    zpool create -f -o autotrim=on "${EXTRA_ZPOOL_OPTS[@]}" \
      tank raidz2 "$(partdev "$tm1" 1)" "$(partdev "$tm2" 1)" "$tank3" "$tank4" \
      "${special_args[@]}"
  fi
  if ! zpool list -H mouse >/dev/null 2>&1; then
    zpool create -f -o autotrim=off "${EXTRA_ZPOOL_OPTS[@]}" \
      mouse mirror "$(partdev "$tm1" 2)" "$(partdev "$tm2" 2)"
  fi
}

create_extra_pools() {
  if [ -z "${EXTRA_POOLS:-}" ]; then
    return 0
  fi

  read -r -a EXTRA_DISK_QUEUE <<<"${EXTRA_DISKS:-}"
  for pool in $EXTRA_POOLS; do
    case "$pool" in
    apoc) create_extra_apoc ;;
    dozer) create_extra_dozer ;;
    zee) create_extra_zee ;;
    tank_mouse) create_extra_tank_mouse ;;
    *)
      echo >&2 "provision.sh: unknown EXTRA_POOLS entry '$pool'"
      exit 1
      ;;
    esac
  done

  mkdir -p /mnt/etc/zfs
  cp /etc/zfs/zpool.cache /mnt/etc/zfs/zpool.cache
}

# Per-disk partition paths, computed once and exported as space-delimited
# strings so chroot.sh consumes them directly without re-running partdev.
# One unified layout for every host (notes/unified_disk_layout.md):
#   1 = BIOS boot (EF02)
#   2 = EFI (EF00)
#   3 = swap (8200)
#   4 = podman store (8300, optional -- when PODMAN_SIZE is set)
#   6 = tank special-vdev member (BF01, mirror only -- when META_SIZE is set)
#   5 = rpool (BF00)
# rpool is always number 5 (single-disk and mirror) and always carved last
# (-n5:0:0) so it grows into the rest of the disk -- a cloud-image deploy grows
# p5 (chroot.sh's hetzner_growpart). The mirror-only meta partition is numbered
# 6 but carved physically *before* rpool, so rpool's number never shifts with
# disk count. Each gets a GPT name (sgdisk -c) for readable lsblk/gdisk output;
# consumers resolve by filesystem UUID, /dev/md path, or pool label, never
# by-partlabel (non-unique across the mirror's identically-named disks) --
# except single-disk swap/podman, where by-partlabel IS unique (one disk).
#
# swap and podman are raw partitions on every host: single-disk gets a bare
# partition, mirror an mdadm array across the per-disk partitions (swap raid1,
# podman raid5 -- chroot.sh). The meta partition becomes tank's special vdev
# (create_extra_tank_mouse). Swap is the disk-backed *overflow* behind zram,
# which the swap role runs as the primary high-priority device
# (notes/swap_strategy.md); a real partition is deadlock-free, unlike swap on a
# zvol.
PARTITIONS_EFI=""
PARTITIONS_SWAP=""
PARTITIONS_PODMAN=""
PARTITIONS_META=""
PARTITIONS_RPOOL=""
for d in $DISKS; do
  PARTITIONS_EFI+="${PARTITIONS_EFI:+ }$(partdev "$d" 2)"
  PARTITIONS_SWAP+="${PARTITIONS_SWAP:+ }$(partdev "$d" 3)"
  if [ -n "${PODMAN_SIZE:-}" ]; then
    PARTITIONS_PODMAN+="${PARTITIONS_PODMAN:+ }$(partdev "$d" 4)"
  fi
  if [ -n "${META_SIZE:-}" ]; then
    PARTITIONS_META+="${PARTITIONS_META:+ }$(partdev "$d" 6)"
  fi
  PARTITIONS_RPOOL+="${PARTITIONS_RPOOL:+ }$(partdev "$d" 5)"
done
export PARTITIONS_EFI PARTITIONS_SWAP PARTITIONS_PODMAN PARTITIONS_META PARTITIONS_RPOOL

export DEBIAN_FRONTEND=noninteractive

# Retry transient apt failures (Nexus restart, packet loss) on the
# build VM. chroot.sh sets the same on the new install.
# Acquire::Retries::Delay (apt 2.7+ in noble) adds backoff between
# attempts so a Nexus restart of a few seconds isn't burned through
# instantly; apt on jammy retries immediately.
echo 'Acquire::Retries "3";' >/etc/apt/apt.conf.d/80-retries
if [ "$UBUNTU_NAME" != "jammy" ]; then
  echo 'Acquire::Retries::Delay "true";' >>/etc/apt/apt.conf.d/80-retries
fi

# apt-get update exits 0 even when one component's Packages index fails to
# download (a Nexus restart, a dropped packet on the build NIC): the partial
# index then makes the install below fail with a baffling "Unable to locate
# package" for whatever the missed component held (e.g. universe). Error-Mode
# =any turns a failed fetch into a non-zero exit; the loop retries with
# backoff so a brief blip is absorbed instead of poisoning the install. jammy
# apt can't do Acquire::Retries::Delay (set above), so the inter-attempt wait
# lives here. Fail loudly only once the attempts are spent.
apt_update() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if apt-get update -o APT::Update::Error-Mode=any; then
      return 0
    fi
    echo "apt-get update attempt ${attempt} failed; retrying in $((attempt * 5))s" >&2
    sleep "$((attempt * 5))"
  done
  echo "apt-get update failed after 5 attempts" >&2
  return 1
}

# Block until cloud-init has finished applying user-data before touching apt.
# preserve_sources_list:false + apt.primary in user-data.pkrtpl rewrite
# sources.list to the Nexus mirror, but that runs in cloud-init's config stage
# -- which is still going when packer's SSH provisioner connects (sshd opens in
# the earlier network stage). Without this wait the apt below races the rewrite
# and falls back to the base image's upstream archive.ubuntu.com; multi-disk
# variants (lab/pug) boot slower and lose the race deterministically, failing
# with "Unable to locate package" once the wipe below drops the primed indices.
# --wait can exit non-zero on a degraded-but-complete run, which is fine here.
cloud-init status --wait || true

# The cloud base image ships a primed /var/lib/apt/lists whose cached base
# jammy InRelease lets apt-get update record a "Hit" and skip re-fetching the
# base suite -- but its Packages files aren't all present, so apt rejects the
# whole base suite and the install below can't locate base packages
# (debootstrap, zfsutils-linux, ...). Wipe the dir so update re-fetches every
# index cleanly -- the same guard write_sources_list applies in chroot.sh.
find /var/lib/apt/lists -type f -delete

# A live ISO adds a cdrom source that has no Release file once the installer
# media is copied into RAM. Remove only that entry: jammy cloud images keep
# their network mirrors in this same file.
if [ -f /etc/apt/sources.list ]; then
  sed -i '\|^[[:space:]]*deb[[:space:]]\+cdrom:|d' /etc/apt/sources.list
fi

apt_update
apt-get install --yes arch-install-scripts debootstrap gdisk zfsutils-linux
if [ "$PRESERVE_META" = true ]; then
  apt-get install --yes mdadm
fi

zgenhostid -f

if [ "$PRESERVE_META_FIXTURE" = true ]; then
  "$SCRIPTS_DIR/preserve_meta_fixture.sh" seed
fi

if [ "$PRESERVE_META" = true ]; then
  # Validate and snapshot all three p6 identities before the first disk is
  # changed. A mismatch on disk three therefore cannot leave disks one and two
  # half-repartitioned.
  capture_preserved_meta
  partition_disks_preserving_meta
  verify_preserved_meta

  if [ "$PRESERVE_META_FIXTURE" = true ]; then
    # Stamp the documented stopped retry state onto the first-pass layout, then
    # exercise the same production cleanup while comparing against the original
    # fixture labels and partition GUIDs.
    "$SCRIPTS_DIR/preserve_meta_fixture.sh" prepare_retry
    partition_disks_preserving_meta
    verify_preserved_meta
  fi
else
  for disk in $DISKS; do
    # Defensive wipe -- a no-op against packer's fresh qcow2s, but necessary on
    # ordinary bare metal. PRESERVE_META takes the partition-scoped path above.
    wipe_disk "$disk"

    sgdisk -a1 -n1:24K:+1000K -t1:EF02 -c1:bios "$disk" # MBR booting (EF02 = BIOS boot partition)
    sgdisk -n2:1M:+1G -t2:EF00 -c2:efi "$disk"          # EFI (EF00 = EFI system partition)

    # Swap partition (p3), sized by SWAP_SIZE, on every host. Single-disk hosts
    # mkswap it directly; mirror hosts mdadm the per-disk p3s into a raid1
    # (chroot.sh). A real partition is deadlock-free, unlike swap on a zvol.
    sgdisk "-n3:0:+$SWAP_SIZE" -t3:8200 -c3:swap "$disk" # Swap (8200 = Linux Swap)

    # Dedicated podman store partition (p4). Single-disk hosts carry a plain
    # ext4 here; mirror hosts mdadm the per-disk p4s into a raid5 (chroot.sh).
    # 8300 = Linux filesystem.
    if [ -n "${PODMAN_SIZE:-}" ]; then
      sgdisk "-n4:0:+$PODMAN_SIZE" -t4:8300 -c4:podman "$disk"
    fi

    # tank special-vdev member (p6, mirror only). Numbered 6 but carved before
    # rpool so rpool stays number 5. mdadm-free -- ZFS mirrors the per-disk p6s
    # into tank's special vdev (create_extra_tank_mouse). BF01 = Solaris /usr &
    # Mac ZFS.
    if [ -n "${META_SIZE:-}" ]; then
      sgdisk "-n6:0:+$META_SIZE" -t6:BF01 -c6:meta "$disk"
    fi

    sgdisk -n5:0:0 -t5:BF00 -c5:rpool "$disk" # rpool (BF00 = Solaris root), carved last so it grows to end

    sgdisk -p "$disk"
  done
fi

# Wait for udev to expose every new partition node (/dev/vdbN, ...)
# before zpool create reads them. Replaces a per-iteration `sync; sleep 2`
# pair that was timing-based cargo for the same goal.
udevadm settle

# Create the zpool. $LAYOUT is "" (single) or "mirror"; $PARTITIONS_RPOOL
# is the space-separated rpool partitions — both intentionally unquoted
# so the shell word-splits them into the zpool args.

# shellcheck disable=SC2086
zpool create -f \
  -o ashift=12 \
  -o autotrim=on \
  -o compatibility=openzfs-2.1-linux \
  -O casesensitivity=sensitive \
  -O normalization=formD \
  -O utf8only=on \
  -O acltype=posix \
  -O atime=on \
  -O canmount=off \
  -O compression=zstd \
  -O dnodesize=auto \
  -O overlay=off \
  -O relatime=on \
  -O xattr=sa \
  -m none \
  rpool $LAYOUT $PARTITIONS_RPOOL

# Create initial file systems

zfs create -o canmount=off -o mountpoint=none rpool/ROOT
zfs create -o canmount=noauto -o mountpoint=/ "rpool/ROOT/$UBUNTU_NAME"

# Swap is a raw partition on every host now (p3; raid1 across the per-disk
# partitions on a mirror -- chroot.sh), not an rpool zvol. A real partition is
# deadlock-free, unlike paging out to a zvol under memory pressure.

zpool set "bootfs=rpool/ROOT/$UBUNTU_NAME" rpool

# Export, then re-import with a temporary mountpoint of /mnt.

zpool_export_retry rpool
zpool import -N -R /mnt rpool
zfs mount "rpool/ROOT/$UBUNTU_NAME"

# Wait for udev to wire the new device nodes before arch-chroot runs.
udevadm settle

# Verify that everything is mounted correctly

mount | grep mnt

# Install Ubuntu. If a fetch fails, --verbose surfaces each retrieve/validate
# step live and the handler dumps debootstrap's own log -- which otherwise dies
# with the build VM -- so the next occurrence stays diagnosable.
debootstrap --verbose "$UBUNTU_NAME" /mnt "$UBUNTU_MIRROR" || {
  rc=$?
  echo "=== debootstrap failed (exit $rc); /mnt/debootstrap/debootstrap.log tail ===" >&2
  tail -n 300 /mnt/debootstrap/debootstrap.log >&2 || echo "(no debootstrap.log present)" >&2
  exit "$rc"
}

# Copy files into the new install. /etc/hostid must match the one ZFS
# saw at pool creation; arch-chroot bind-mounts /etc/resolv.conf so apt
# inside the chroot can resolve hostnames, and the bind goes away when
# arch-chroot exits — the shipped image keeps whatever debootstrap put
# there (empty), not the build host's DNS settings.

cp /etc/hostid /mnt/etc

# A disposable image build (qemu test fixture or the Hetzner snapshot) is
# either re-baked on failure or grown + verified on first boot; a bare-metal
# copy-paste run installs onto the real host. Every packer source sets one of
# the two flags below, so "neither" is exactly the bare-metal path. Gates the
# build-only speed hacks that trade durability for wall-clock.
building_image=false
if [ "${QEMU_TEST_IMAGE:-false}" = "true" ] || [ "${IMAGE_TARGET:-qemu}" = "hetzner" ]; then
  building_image=true
fi

# Build-time dpkg I/O mode: dpkg fsyncs every unpacked file by default, which
# dominates the chroot install's wall-clock on network-backed disks (EBS on the
# AWS bake) and still costs plenty locally. A disposable build that crashes
# mid-install is re-baked, never booted, so per-file durability buys nothing --
# and the image is quiesced by the zpool export below regardless. Skipped on a
# bare-metal install: an interrupted provision is costlier to redo, the
# local-SSD speed win is small, and fsync durability is worth keeping. Removed
# before the image is sealed.
if [ "$building_image" = "true" ]; then
  echo force-unsafe-io >/mnt/etc/dpkg/dpkg.cfg.d/90-build-unsafe-io
fi

# Stage the Hetzner cloud-init drop-in for this release so chroot.sh can install
# it into /etc/cloud/cloud.cfg.d, making the image behave like the stock hcloud
# image (mirror.hetzner.com apt, Hetzner module set). Under /var/tmp, not /tmp:
# arch-chroot shadows the chroot's /tmp with a private tmpfs, hiding files
# pre-staged there. Skipped on the qemu fixtures and the bare-metal path (no
# hetzner dir, IMAGE_TARGET != hetzner).
if [ "${IMAGE_TARGET:-qemu}" = "hetzner" ]; then
  install -D -m 0644 "$SCRIPTS_DIR/hetzner/90-hetznercloud.cfg.$UBUNTU_NAME" /mnt/var/tmp/90-hetznercloud.cfg
fi

# Chroot into the new OS via arch-chroot (arch-install-scripts). It
# bind-mounts proc/sys/dev/devpts/run/efivarfs and /etc/resolv.conf
# under /mnt for the chroot's lifetime, so apt can resolve hostnames
# during the install without leaving the build host's DNS pinned in
# the shipped image.
#
# arch-chroot mounts directly into the host namespace, not a private
# one, so any mount the chroot script adds (notably /boot/efi from
# chroot.sh) leaks into the host and would block the later zfs
# unmount /mnt. Wrap in `unshare --mount --propagation private` so
# everything mounted between here and exit lives in a throw-away
# namespace that's destroyed when unshare returns.
#
# Env propagation: arch-chroot inherits the calling shell's env, so
# packer's UBUNTU_*/ZBM_*/REFIND_NAME/SSH_KEY_PUB (already exported via
# the shell provisioner env block) flow straight through. Script-local
# vars must be exported explicitly. DISKS rides as a space-delimited
# string (not a bash array, which bash refuses to put in env); chroot.sh
# consumes it the same way via unquoted `for d in $DISKS` word-splitting.
unshare --mount --propagation private arch-chroot /mnt bash <"$SCRIPTS_DIR/chroot.sh"

if [ "$building_image" = "true" ]; then
  rm /mnt/etc/dpkg/dpkg.cfg.d/90-build-unsafe-io
fi

if [ "$PRESERVE_META" = true ]; then
  # The complete install, including mdadm creation in chroot.sh, must leave the
  # three p6 GPT entries and every readable ZFS label unchanged.
  verify_preserved_meta
fi

# Create any non-rpool pools while /mnt is still rpool's root so the current
# zpool.cache can be copied into the shipped install.
create_extra_pools

# Only the rpool root dataset itself remains mounted in the host namespace.
zfs unmount "rpool/ROOT/$UBUNTU_NAME"
sync

# Export every pool (zpool_export_retry handles the "pool is busy"
# udev/systemd race; see its comment). Extra pools go first; rpool last
# because the zfs unmount above has already quiesced its root dataset.
for pool in $(zpool list -H -o name | grep -vx rpool); do
  zpool_export_retry "$pool"
done
zpool_export_retry rpool
