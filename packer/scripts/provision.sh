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
# INSTALL_TARGET, UBUNTU_NAME, and UBUNTU_MIRROR
# come from packer's shell-provisioner env block. Bare-metal callers export
# them by hand.
# This script consumes the disk/pool vars and passes the exported install vars
# through to chroot.sh. The ZBM_*/UBUNTU_MIRROR_* vars used downstream
# are documented at the top of chroot.sh.

# Directories holding chroot.sh, optional installer extensions, and the four
# role-owned bootstrap files. Packer uploads them to /home/vagrant; bare-metal
# callers can point both variables at their staged bundle.
SCRIPTS_DIR="${SCRIPTS_DIR:-/home/vagrant}"
ROLE_FILES_DIR="${ROLE_FILES_DIR:-$SCRIPTS_DIR}"
ROLE_FILES=(console-setup keyboard modules_most zz-stage-efi-stub)

preflight() {
  local name disk role_file
  local -A seen_disks=()
  local required=(
    DISKS EXTRA_DISKS LAYOUT SWAP_SIZE PODMAN_SIZE META_SIZE EXTRA_POOLS
    UBUNTU_NAME UBUNTU_MIRROR UBUNTU_MIRROR_SECURITY
    UBUNTU_MIRROR_UPSTREAM UBUNTU_MIRROR_SECURITY_UPSTREAM
    ZBM_VERSION
  )

  for name in "${required[@]}"; do
    if ! [[ -v $name ]]; then
      echo "provision.sh: required variable $name is not set" >&2
      return 1
    fi
  done

  for name in DISKS SWAP_SIZE UBUNTU_NAME UBUNTU_MIRROR UBUNTU_MIRROR_SECURITY UBUNTU_MIRROR_UPSTREAM UBUNTU_MIRROR_SECURITY_UPSTREAM ZBM_VERSION; do
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
  case $INSTALL_TARGET in bare_metal | qemu | hetzner) ;; *)
    echo "provision.sh: INSTALL_TARGET must be bare_metal, qemu, or hetzner (got '$INSTALL_TARGET')" >&2
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

  if [ "$INSTALL_TARGET" != hetzner ]; then
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

  for role_file in "${ROLE_FILES[@]}"; do
    if [ ! -f "$ROLE_FILES_DIR/$role_file" ]; then
      echo "provision.sh: required role file not found: $ROLE_FILES_DIR/$role_file" >&2
      return 1
    fi
  done
}

# Packer selects qemu or hetzner; direct callers get the bare-metal path.
export INSTALL_TARGET="${INSTALL_TARGET:-bare_metal}"

preflight

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
    if [ "${#EXTRA_DISK_QUEUE[@]}" -eq 0 ]; then
      echo >&2 "provision.sh: ran out of EXTRA_DISKS while allocating $n for $pool"
      exit 1
    fi
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
    *)
      echo >&2 "provision.sh: unknown EXTRA_POOLS entry '$pool'"
      exit 1
      ;;
    esac
  done

  # zpool.cache records the device paths a pool was imported from, and the
  # build VM carries its own root disk ahead of the target disks: /dev/vdX
  # here is one letter further along than in a machine booted from the
  # image alone. The cached import then fails on every first boot and falls
  # back to a full device scan. Re-import by partition UUID, which survives
  # the renumbering. A bare-metal install keeps its own paths -- they are
  # already the paths that host boots with.
  if [ "$INSTALL_TARGET" != bare_metal ]; then
    for pool in $(zpool list -H -o name | grep -vx rpool); do
      zpool_export_retry "$pool"
      zpool import -d /dev/disk/by-partuuid -N "$pool"
    done
  fi

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

# Apt retries individual downloads itself. Error-Mode=any additionally rejects
# a partial index update, and this outer backoff absorbs a longer mirror outage
# before the package install encounters misleading missing-package failures.
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
# InRelease lets apt-get update record a "Hit" and skip re-fetching the base
# suite -- but its Packages files aren't all present, so apt rejects the whole
# base suite and the install below can't locate base packages
# (debootstrap, zfsutils-linux, ...). Wipe the dir so update re-fetches every
# index cleanly -- the same guard write_sources_list applies in chroot.sh.
find /var/lib/apt/lists -type f -delete

# A live ISO adds a cdrom source that has no Release file once the installer
# media is copied into RAM. Remove only that entry so any network mirrors in
# the same file survive.
if [ -f /etc/apt/sources.list ]; then
  sed -i '\|^[[:space:]]*deb[[:space:]]\+cdrom:|d' /etc/apt/sources.list
fi

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

# Export, then re-import with a temporary mountpoint of /mnt.

zpool_export_retry rpool
zpool import -N -R /mnt rpool
zfs mount "rpool/ROOT/$UBUNTU_NAME"

# Wait for udev to wire the new device nodes before arch-chroot runs.
udevadm settle

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

# Build-time dpkg I/O mode: dpkg fsyncs every unpacked file by default, which
# dominates the chroot install's wall-clock on network-backed disks (EBS on the
# AWS bake) and still costs plenty locally. A disposable build that crashes
# mid-install is re-baked, never booted, so per-file durability buys nothing --
# and the image is quiesced by the zpool export below regardless. Skipped on a
# bare-metal install: an interrupted provision is costlier to redo, the
# local-SSD speed win is small, and fsync durability is worth keeping. Removed
# before the image is sealed.
if [ "$INSTALL_TARGET" != bare_metal ]; then
  echo force-unsafe-io >/mnt/etc/dpkg/dpkg.cfg.d/90-build-unsafe-io
fi

# Stage the four role-owned bootstrap files inside the target root so chroot.sh
# consumes the same bytes as Ansible without installing Ansible there.
CHROOT_ROLE_FILES=/var/tmp/homelab-role-files
cleanup_chroot_role_files() {
  rm -rf -- "/mnt${CHROOT_ROLE_FILES:?}"
}
trap cleanup_chroot_role_files EXIT
cleanup_chroot_role_files
mkdir -p "/mnt$CHROOT_ROLE_FILES"
for role_file in "${ROLE_FILES[@]}"; do
  cp "$ROLE_FILES_DIR/$role_file" "/mnt$CHROOT_ROLE_FILES/"
done
export CHROOT_ROLE_FILES

# Stage the Hetzner image setup (cloud-init drop-in for this release,
# datasource pin, first-boot growpart unit) so chroot.sh can run it inside
# the target root. Under /var/tmp, not /tmp: arch-chroot shadows the
# chroot's /tmp with a private tmpfs, hiding files pre-staged there. Skipped
# on the qemu fixtures and the bare-metal path.
if [ "$INSTALL_TARGET" = "hetzner" ]; then
  cp -a "$SCRIPTS_DIR/hetzner" /mnt/var/tmp/hetzner
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
# the shell provisioner env block) flow straight through. CHROOT_ROLE_FILES is
# exported above. DISKS rides as a space-delimited string (not a bash array,
# which bash refuses to put in env); chroot.sh consumes it the same way via
# unquoted `for d in $DISKS` word-splitting.
unshare --mount --propagation private arch-chroot /mnt bash <"$SCRIPTS_DIR/chroot.sh"

cleanup_chroot_role_files
trap - EXIT

if [ "$INSTALL_TARGET" != bare_metal ]; then
  rm /mnt/etc/dpkg/dpkg.cfg.d/90-build-unsafe-io
fi

# Create any non-rpool pools while /mnt is still rpool's root so the current
# zpool.cache can be copied into the shipped install.
create_extra_pools

# A freshly created array resyncs in the background, and these are built
# without a write-intent bitmap. Sealing the image mid-resync makes every
# machine booted from it redo the whole resync -- on a CI host that is a
# 26 GiB RAID5 rebuild per cell, competing with the converge it just
# started. Wait once here instead. mdadm --wait exits 1 when an array has
# nothing in flight, which is the common case for the small mirrors.
if [ "$INSTALL_TARGET" != bare_metal ]; then
  for md in /dev/md/efi /dev/md/swap /dev/md/podman; do
    [ -e "$md" ] || continue
    mdadm --wait "$md" || true
  done
fi

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
