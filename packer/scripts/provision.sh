#!/bin/bash
# Bootstrap a ZFS-on-root install onto $DISKS. Used by packer's qemu
# build and as the bare-metal copy-paste path for provisioning new
# lab-class hosts.
#
# Bare-metal callers MUST also:
#  - pre-flight $DISKS. Every entry is wiped unconditionally (sgdisk --zap-all
#    + wipefs + blkdiscard + zpool labelclear); a wrong device path destroys
#    data in seconds.
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

# DISKS, EXTRA_DISKS, LAYOUT, SWAP_SIZE, PODMAN_SIZE, META_SIZE, EXTRA_POOLS,
# INSTALL_TARGET, UBUNTU_NAME, and the optional NEXUS_URL come from packer's
# shell-provisioner env block. Bare-metal callers export them by hand.
# This script consumes the disk/pool vars, derives the UBUNTU_MIRROR* URLs, and
# passes the exported install vars through to chroot.sh. The ZBM_* vars used
# downstream are documented at the top of chroot.sh.

# Directories holding chroot.sh, optional installer extensions, and the
# role-owned bootstrap files. Packer uploads them to /home/vagrant; bare-metal
# callers can point both variables at their staged bundle.
SCRIPTS_DIR="${SCRIPTS_DIR:-/home/vagrant}"
ROLE_FILES_DIR="${ROLE_FILES_DIR:-$SCRIPTS_DIR}"
# shellcheck source=packer/scripts/install_lib.sh
. "$SCRIPTS_DIR/install_lib.sh"

preflight() {
  local disk
  local -A seen_disks=()

  # Optional layout features may be empty but must be set explicitly.
  : "${EXTRA_DISKS?}" "${LAYOUT?}" "${PODMAN_SIZE?}" "${META_SIZE?}" "${EXTRA_POOLS?}"
  : "${DISKS:?}" "${SWAP_SIZE:?}" "${UBUNTU_NAME:?}" "${ZBM_VERSION:?}"

  [[ $LAYOUT =~ ^(mirror)?$ ]] || fail "LAYOUT must be empty or mirror (got '$LAYOUT')"
  [[ $INSTALL_TARGET =~ ^(bare_metal|qemu|hetzner)$ ]] ||
    fail "INSTALL_TARGET must be bare_metal, qemu, or hetzner (got '$INSTALL_TARGET')"
  [ "${TARGET_HOSTNAME+set}" = "${TARGET_USERNAME+set}" ] ||
    fail "TARGET_HOSTNAME and TARGET_USERNAME must be set together"
  [[ ! -v TARGET_HOSTNAME || $TARGET_HOSTNAME =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] ||
    fail "invalid TARGET_HOSTNAME '$TARGET_HOSTNAME'"
  [[ ! -v TARGET_USERNAME || $TARGET_USERNAME =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] ||
    fail "invalid TARGET_USERNAME '$TARGET_USERNAME'"
  [[ $INSTALL_TARGET == hetzner || ${SSH_KEY_PUB-} =~ ^(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521))[[:space:]] ]] ||
    fail "SSH_KEY_PUB must contain a supported public key"

  for disk in $DISKS $EXTRA_DISKS; do
    [[ $disk == /dev/* ]] || fail "disk path must start with /dev/ (got '$disk')"
    [[ ! -v seen_disks[$disk] ]] || fail "disk '$disk' is listed more than once"
    seen_disks[$disk]=1
  done

  check_role_files
}

# Packer selects qemu or hetzner; direct callers get the bare-metal path.
export INSTALL_TARGET="${INSTALL_TARGET:-bare_metal}"

preflight

export_ubuntu_mirrors

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

wipe_disks() {
  local device devtype disk

  for disk in "$@"; do
    zpool labelclear -f "$disk" || true

    # Whole-disk wipefs removes the partition table, not signatures stored
    # inside its partitions. Clear those first, then replace the table.
    # labelclear per partition too: libblkid's zfs_member probe reads only
    # the front-of-device labels, so a previous install's end-of-device ZFS
    # labels survive wipefs and stay visible to a later `zpool import -f`.
    while read -r device devtype; do
      if [ "$devtype" = part ]; then
        zpool labelclear -f "$device" || true
        wipefs --all --force "$device"
      fi
    done < <(lsblk --raw --noheadings --paths --output NAME,TYPE "$disk")

    wipefs --all --force "$disk"
    blkdiscard -f "$disk" || true
    sgdisk --zap-all "$disk"
    # sgdisk rewrote the on-disk table; drop the kernel's stale entries for it.
    # partx --update alone only revisits partition numbers present in the new
    # table, leaving nodes above the new count registered and claimable by a
    # later zpool/mdadm create.
    partx --delete "$disk" || true
  done
}

partition_disk() {
  local disk="$1"

  sgdisk -a1 -n1:24K:+1000K -t1:EF02 -c1:bios "$disk" # MBR booting (EF02 = BIOS boot partition)
  sgdisk -n2:1M:+1G -t2:EF00 -c2:efi "$disk"          # EFI (EF00 = EFI system partition)

  # Swap partition (p3), sized by SWAP_SIZE, on every host. Single-disk hosts
  # mkswap it directly; mirror hosts mdadm the per-disk p3s into a raid1
  # (chroot.sh). A real partition is deadlock-free, unlike swap on a zvol.
  sgdisk "-n3:0:+$SWAP_SIZE" -t3:8200 -c3:swap "$disk" # Swap (8200 = Linux Swap)

  # Dedicated podman store partition (p4). Single-disk hosts carry a plain
  # ext4 here; mirror hosts mdadm the per-disk p4s into a raid5 (chroot.sh).
  # 8300 = Linux filesystem.
  if [ -n "$PODMAN_SIZE" ]; then
    sgdisk "-n4:0:+$PODMAN_SIZE" -t4:8300 -c4:podman "$disk"
  fi

  # tank special-vdev member (p6, mirror only). Numbered 6 but carved before
  # rpool so rpool stays number 5. mdadm-free -- ZFS mirrors the per-disk p6s
  # into tank's special vdev (create_extra_tank_mouse). BF01 = Solaris /usr &
  # Mac ZFS.
  if [ -n "$META_SIZE" ]; then
    sgdisk "-n6:0:+$META_SIZE" -t6:BF01 -c6:meta "$disk"
  fi

  sgdisk -n5:0:0 -t5:BF00 -c5:rpool "$disk" # rpool (BF00 = Solaris root), carved last so it grows to end
  sgdisk -p "$disk"
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
    ((${#EXTRA_DISK_QUEUE[@]})) || fail "ran out of EXTRA_DISKS while allocating $n for $pool"
    POPPED_EXTRA_DISKS+=("${EXTRA_DISK_QUEUE[0]}")
    EXTRA_DISK_QUEUE=("${EXTRA_DISK_QUEUE[@]:1}")
  done
}

# Keep the apoc and dozer builders separate: each documents the storage
# layout of a physical rack host, including its intentional autotrim policy.
create_extra_apoc() {
  pop_extra_disks 2 apoc
  local extra_pool_disks=("${POPPED_EXTRA_DISKS[@]}")
  if zpool list -H apoc >/dev/null 2>&1; then return; fi
  wipe_disks "${extra_pool_disks[@]}"
  udevadm settle
  zpool create -f -o autotrim=off "${EXTRA_ZPOOL_OPTS[@]}" apoc mirror "${extra_pool_disks[@]}"
}

create_extra_dozer() {
  pop_extra_disks 2 dozer
  local extra_pool_disks=("${POPPED_EXTRA_DISKS[@]}")
  if zpool list -H dozer >/dev/null 2>&1; then return; fi
  wipe_disks "${extra_pool_disks[@]}"
  udevadm settle
  zpool create -f -o autotrim=on "${EXTRA_ZPOOL_OPTS[@]}" dozer mirror "${extra_pool_disks[@]}"
}

create_extra_tank_mouse() {
  pop_extra_disks 4 tank_mouse
  local extra_pool_disks=("${POPPED_EXTRA_DISKS[@]}")
  local tm1=${POPPED_EXTRA_DISKS[0]} tm2=${POPPED_EXTRA_DISKS[1]} tank3=${POPPED_EXTRA_DISKS[2]} tank4=${POPPED_EXTRA_DISKS[3]}
  wipe_disks "${extra_pool_disks[@]}"
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
    # tank then has no special vdev. See notes/archive/special-vdev-sizing.md.
    local special_args=()
    if [ -n "$PARTITIONS_META" ]; then
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
  if [ -z "$EXTRA_POOLS" ]; then
    return 0
  fi

  read -r -a EXTRA_DISK_QUEUE <<<"$EXTRA_DISKS"
  for pool in $EXTRA_POOLS; do
    case "$pool" in
    apoc) create_extra_apoc ;;
    dozer) create_extra_dozer ;;
    tank_mouse) create_extra_tank_mouse ;;
    *) fail "unknown EXTRA_POOLS entry '$pool'" ;;
    esac
  done

  # zpool create recorded the kernel device paths it was given; re-import
  # from POOL_DEVICE_DIR (set beside rpool's import) so zpool.cache records
  # stable names.
  for pool in $(zpool list -H -o name | grep -vx rpool); do
    zpool_export_retry "$pool"
    zpool import -d "$POOL_DEVICE_DIR" -N "$pool"
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
# p5 (cloud-init growpart, packer/hetzner). The mirror-only meta partition is numbered
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
  if [ -n "$PODMAN_SIZE" ]; then
    PARTITIONS_PODMAN+="${PARTITIONS_PODMAN:+ }$(partdev "$d" 4)"
  fi
  if [ -n "$META_SIZE" ]; then
    PARTITIONS_META+="${PARTITIONS_META:+ }$(partdev "$d" 6)"
  fi
  PARTITIONS_RPOOL+="${PARTITIONS_RPOOL:+ }$(partdev "$d" 5)"
done
export PARTITIONS_EFI PARTITIONS_SWAP PARTITIONS_PODMAN PARTITIONS_META PARTITIONS_RPOOL

export DEBIAN_FRONTEND=noninteractive

# cloud-init's apt module rewrites the build VM's sources in its config stage,
# which is still running when packer's SSH provisioner connects (sshd opens in
# the earlier network stage); let it finish before replacing them. --wait can
# exit non-zero on a degraded-but-complete run, and a live ISO may lack
# cloud-init entirely; neither matters here.
cloud-init status --wait || true

# Install the tools below from the same mirror as the target. Replacing the
# live environment's sources (for its own release) also drops a live ISO's
# cdrom entry.
# shellcheck source=/dev/null  # the live environment's own release file
write_sources_list "$(. /etc/os-release && echo "$VERSION_CODENAME")" "$UBUNTU_MIRROR" "$UBUNTU_MIRROR_SECURITY"

apt_update
(
  # mdadm and zfsutils-linux both start storage units from their package
  # postinst. Hold those units while the live environment still has its stock
  # mdadm policy and the target disks may carry stale pool/array metadata.
  printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d
  chmod 0755 /usr/sbin/policy-rc.d
  trap 'rm -f /usr/sbin/policy-rc.d' EXIT
  apt-get install --yes arch-install-scripts debootstrap gdisk mdadm zfsutils-linux
)

# Stop udev's `mdadm --incremental` from re-assembling an array off a stale
# superblock while the tables below are rewritten. Declarative, so it cannot
# leak the way a stop-exec-queue trap can if the script exits mid-partition.
# Live build environment only -- the shipped image's mdadm.conf is written
# independently by chroot.sh.
printf 'AUTO -all\n' >/etc/mdadm/mdadm.conf

zgenhostid -f

# Stale md members on the target disks would let udev's `mdadm --incremental`
# re-assemble an array mid-partitioning and hold a member busy. Tear those
# arrays down once before partitioning; later wipe_disks calls prepare
# extra-pool disks after the new EFI, swap, and Podman arrays already exist.
#
# Scoped to the disks this run owns, never `--stop --scan` / an unqualified
# lsblk: on the bare-metal path the machine may carry arrays on disks the
# caller deliberately left out of DISKS/EXTRA_DISKS, and zeroing their
# superblocks is not recoverable.
# shellcheck disable=SC2086  # word-splitting on the disk lists is the point
mapfile -t md_members < <(lsblk --raw --noheadings --paths --output NAME,FSTYPE $DISKS $EXTRA_DISKS | awk '$2 == "linux_raid_member" { print $1 }')
if ((${#md_members[@]})); then
  # Stop the arrays those members back before clearing them -- mdadm refuses
  # to zero a superblock while its array is still assembled.
  mapfile -t md_holders < <(lsblk --raw --noheadings --paths --output NAME,TYPE "${md_members[@]}" | awk '$2 ~ /^raid/ { print $1 }' | sort -u)
  for md_holder in "${md_holders[@]}"; do
    mdadm --stop "$md_holder" || true
  done
  mdadm --zero-superblock "${md_members[@]}"
fi

# Defensive wipe -- a no-op against fresh Packer qcow2s, but necessary on
# ordinary bare metal. DISKS is intentionally split into device arguments.
# shellcheck disable=SC2086
wipe_disks $DISKS
for disk in $DISKS; do
  partition_disk "$disk"
done
# Publish each completed table through util-linux rather than relying on
# partprobe being present in the live environment.
for disk in $DISKS; do
  partx --update "$disk"
done

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

# Pools are re-imported from stable device names, which zpool.cache records
# for later imports. Physical disks carry serials, so by-id names them across
# port and controller changes. Virtual disks have none, and the build VM's own
# root disk shifts /dev/vdX one letter against a machine booted from the image
# alone, so VMs use the partition UUID.
if [ "$INSTALL_TARGET" = bare_metal ]; then
  POOL_DEVICE_DIR=/dev/disk/by-id
else
  POOL_DEVICE_DIR=/dev/disk/by-partuuid
fi

# Export, then re-import with a temporary mountpoint of /mnt. An altroot
# import implies cachefile=none, so name the cache explicitly: the shipped
# zpool.cache (copied in create_extra_pools) then lists rpool alongside the
# data pools.

zpool_export_retry rpool
zpool import -N -R /mnt -o cachefile=/etc/zfs/zpool.cache -d "$POOL_DEVICE_DIR" rpool
zfs mount "rpool/ROOT/$UBUNTU_NAME"

# Wait for udev to wire the new device nodes before arch-chroot runs.
udevadm settle

debootstrap_release "$UBUNTU_NAME" /mnt

# Copy files into the new install. /etc/hostid must match the one ZFS
# saw at pool creation; arch-chroot bind-mounts /etc/resolv.conf so apt
# inside the chroot can resolve hostnames, and the bind goes away when
# arch-chroot exits — the shipped image keeps whatever debootstrap put
# there (empty), not the build host's DNS settings.

cp /etc/hostid /mnt/etc

# Env propagation: packer's UBUNTU_*/ZBM_*/REFIND_*/SSH_KEY_PUB (exported via
# the shell provisioner env block) and the PARTITIONS_*/DISKS strings above
# flow through to chroot.sh; chroot.sh consumes DISKS via unquoted
# `for d in $DISKS` word-splitting.
run_chroot_script /mnt

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
