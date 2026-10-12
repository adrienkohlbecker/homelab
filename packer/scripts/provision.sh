#!/bin/bash
# Bootstrap a ZFS-on-root install onto $DISKS. Used by packer's qemu
# build and as the bare-metal copy-paste path for provisioning new
# lab-class hosts.
#
# Bare-metal callers MUST also:
#  - pre-flight $DISKS and $EXTRA_DISKS. Every entry is wiped unconditionally
#    (sgdisk --zap-all + wipefs + blkdiscard + zpool labelclear); a wrong
#    device path destroys data in seconds. Pools imported in the live
#    environment make the script refuse to start.
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

# shellcheck source=packer/scripts/install_lib.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/install_lib.sh"

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
  check_identity

  for disk in $DISKS $EXTRA_DISKS; do
    [[ $disk == /dev/* ]] || fail "disk path must start with /dev/ (got '$disk')"
    [[ ! -v seen_disks[$disk] ]] || fail "disk '$disk' is listed more than once"
    seen_disks[$disk]=1
  done

  check_role_files
}

# Packer selects qemu or hetzner; direct callers get the bare-metal path.
export INSTALL_TARGET="${INSTALL_TARGET:-bare_metal}"

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

preflight

export_ubuntu_mirrors

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
  # (create_storage). A real partition is deadlock-free, unlike swap on a zvol.
  sgdisk "-n3:0:+$SWAP_SIZE" -t3:8200 -c3:swap "$disk" # Swap (8200 = Linux Swap)

  # Dedicated podman store partition (p4). Single-disk hosts carry a plain
  # ext4 here; mirror hosts mdadm the per-disk p4s into a raid5
  # (create_storage).
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

# Assemble the per-disk ESP, swap, and Podman partitions into md arrays on a
# mirror, create the ESP and swap filesystems, and record them in the new
# root's fstab and mdadm.conf.
create_storage() {
  local efi_device swap_device disks_count
  local mdadm_create_opts=() arrays=()
  disks_count=$(wc -w <<<"$DISKS")

  if [ "$LAYOUT" = "" ]; then
    efi_device="$PARTITIONS_EFI"
    swap_device="$PARTITIONS_SWAP"
  else
    # Image builds write to fresh qcow2s that read back as zeros, and all-zero
    # members are already consistent: RAID1 halves match and RAID5 parity of
    # zeros is zero. Skip the initial resync rather than wait on it before
    # sealing. Bare metal keeps it -- its disks hold arbitrary old data.
    if [ "$INSTALL_TARGET" != bare_metal ]; then
      mdadm_create_opts+=(--assume-clean)
    fi

    # Metadata 1.0 and no bitmap keep each RAID1 member recognizable as an ESP.
    # Some firmware may still reject a member; per-disk NVRAM entries below retain
    # alternate boot paths. Validate this layout on each new bare-metal platform.
    # shellcheck disable=SC2086  # word-splitting on PARTITIONS_EFI is the point
    mdadm --create "${mdadm_create_opts[@]}" /dev/md/efi --name=efi --metadata=1.0 --level="raid1" --bitmap=none --raid-devices="$disks_count" $PARTITIONS_EFI
    udevadm settle --timeout=10
    efi_device=/dev/md/efi

    # Disk-backed RAID1 swap avoids the memory-pressure deadlock risk of a ZFS
    # zvol. mdadm.conf makes it available to the initramfs and swapon at boot.
    # shellcheck disable=SC2086  # word-splitting on PARTITIONS_SWAP is the point
    mdadm --create "${mdadm_create_opts[@]}" /dev/md/swap --name=swap --metadata=1.2 --level=raid1 --bitmap=none --raid-devices="$disks_count" $PARTITIONS_SWAP
    udevadm settle --timeout=10
    swap_device=/dev/md/swap

    # The reconstructible Podman store uses RAID5 without a write-intent bitmap:
    # accept a full replacement resync in exchange for lower write amplification.
    # Record the array for boot-time assembly; the Podman role formats and mounts
    # /dev/md/podman.
    if [ -n "$PARTITIONS_PODMAN" ]; then
      # shellcheck disable=SC2086  # word-splitting on PARTITIONS_PODMAN is the point
      mdadm --create "${mdadm_create_opts[@]}" /dev/md/podman --force --name=podman --metadata=1.2 --level=raid5 --bitmap=none --raid-devices="$disks_count" $PARTITIONS_PODMAN
      udevadm settle --timeout=10
    fi

    # The Debian package's default policy (mdmonitor needs MAILADDR) plus the
    # arrays. No role manages this file, and the image's initramfs embeds it.
    arrays=(/dev/md/efi /dev/md/swap)
    if [ -n "$PARTITIONS_PODMAN" ]; then
      arrays+=(/dev/md/podman)
    fi
    mkdir -p /mnt/etc/mdadm
    {
      printf 'HOMEHOST <system>\nMAILADDR root\n'
      mdadm --detail --brief "${arrays[@]}"
    } >/mnt/etc/mdadm/mdadm.conf
  fi

  mkdosfs -F 32 -s 1 -n EFI "$efi_device"
  mkswap -f "$swap_device"

  # UUIDs exist only after the filesystems above have been created; blkid
  # reads the signatures back through the same page cache they were written
  # with, so no settle is needed.
  if [ "$LAYOUT" = "" ]; then
    efi_device="/dev/disk/by-uuid/$(blkid -s UUID -o value "$efi_device")"
    swap_device="/dev/disk/by-uuid/$(blkid -s UUID -o value "$swap_device")"
  fi

  # Keep the ESP mount policy in sync with boot_esp_mount_opts in
  # group_vars/all/main.yml.
  echo "$efi_device /boot/efi vfat defaults,umask=0077 0 0" >>/mnt/etc/fstab
  echo "$swap_device none swap discard 0 0" >>/mnt/etc/fstab
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

create_extra_dozer() {
  pop_extra_disks 2 dozer
  zpool create -f -o autotrim=on "${EXTRA_ZPOOL_OPTS[@]}" dozer mirror "${POPPED_EXTRA_DISKS[@]}"
}

create_extra_tank_mouse() {
  pop_extra_disks 4 tank_mouse
  local tm1=${POPPED_EXTRA_DISKS[0]} tm2=${POPPED_EXTRA_DISKS[1]} tank3=${POPPED_EXTRA_DISKS[2]} tank4=${POPPED_EXTRA_DISKS[3]}
  local special_args=()
  for tm in "$tm1" "$tm2"; do
    sgdisk -n1:0:+1014M -t1:BF01 "$tm"
    sgdisk -n2:0:-8M -t2:BF01 "$tm"
    sgdisk -n3:0:0 -t3:BF07 "$tm"
    sgdisk -p "$tm"
  done
  udevadm settle

  # tank's special vdev lives on the fast NVMe rpool-mirror disks (their p6
  # meta partitions, $PARTITIONS_META), not on tank's own slow raidz2 HDDs --
  # so tank metadata + small-block datasets (special_small_blocks, set per
  # dataset by the zfs role) land on NVMe. A mirror across all the meta
  # partitions tolerates the same disk loss as the raidz2 (losing the special
  # vdev loses the pool). Empty on single-disk hosts (no meta partition), so
  # tank then has no special vdev. See notes/archive/special-vdev-sizing.md.
  if [ -n "$PARTITIONS_META" ]; then
    # shellcheck disable=SC2206  # word-split PARTITIONS_META into vdev members
    special_args=(special mirror $PARTITIONS_META)
  fi
  zpool create -f -o autotrim=on "${EXTRA_ZPOOL_OPTS[@]}" \
    tank raidz2 "$(partdev "$tm1" 1)" "$(partdev "$tm2" 1)" "$tank3" "$tank4" \
    "${special_args[@]}"
  zpool create -f -o autotrim=off "${EXTRA_ZPOOL_OPTS[@]}" \
    mouse mirror "$(partdev "$tm1" 2)" "$(partdev "$tm2" 2)"
}

create_extra_pools() {
  local pool
  read -r -a EXTRA_DISK_QUEUE <<<"$EXTRA_DISKS"
  for pool in $EXTRA_POOLS; do
    case "$pool" in
    dozer) create_extra_dozer ;;
    tank_mouse) create_extra_tank_mouse ;;
    *) fail "unknown EXTRA_POOLS entry '$pool'" ;;
    esac
  done
}

# Per-disk partition paths, computed once as space-delimited strings.
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
# podman raid5 -- create_storage). The meta partition becomes tank's special vdev
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
  apt-get install --yes arch-install-scripts debootstrap dosfstools gdisk mdadm zfsutils-linux
)

# Stop udev's `mdadm --incremental` from re-assembling an array off a stale
# superblock while the tables below are rewritten. Declarative, so it cannot
# leak the way a stop-exec-queue trap can if the script exits mid-partition.
# Live build environment only -- create_storage writes the shipped image's
# mdadm.conf separately.
printf 'AUTO -all\n' >/etc/mdadm/mdadm.conf

zgenhostid -f

# Every pool is created fresh on wiped disks, so one already imported in the
# live environment is a mistake: its disks are either listed here and about to
# be wiped, or another pool would collide with its name.
imported_pools=$(zpool list -H -o name)
[ -z "$imported_pools" ] ||
  fail "export the pools imported in the live environment first: $imported_pools"

# Stale md members on the target disks would let udev's `mdadm --incremental`
# re-assemble an array mid-partitioning and hold a member busy. Tear those
# arrays down before wiping.
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
# ordinary bare metal. The disk lists are intentionally split into device
# arguments.
# shellcheck disable=SC2086
wipe_disks $DISKS $EXTRA_DISKS
for disk in $DISKS; do
  partition_disk "$disk"
done
# Publish each completed table through util-linux rather than relying on
# partprobe being present in the live environment.
for disk in $DISKS; do
  partx --update "$disk"
done

# Wait for udev to expose every new partition node (/dev/vdbN, ...)
# before zpool create reads them.
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

# Every boot environment under rpool/ROOT inherits the command line.
zfs set org.zfsbootmenu:commandline="$(boot_commandline)" rpool/ROOT

zpool set "bootfs=rpool/ROOT/$UBUNTU_NAME" rpool

create_extra_pools

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

# zpool create recorded the kernel device paths it was given; export every
# pool and re-import it from POOL_DEVICE_DIR so zpool.cache records stable
# names. rpool comes back with a temporary mountpoint of /mnt. An altroot
# import implies cachefile=none, so name the cache explicitly: the shipped
# zpool.cache then lists rpool alongside the data pools.
extra_pools=$(zpool list -H -o name | grep -vx rpool || true)
for pool in $extra_pools rpool; do
  zpool_export_retry "$pool"
done
zpool import -N -R /mnt -o cachefile=/etc/zfs/zpool.cache -d "$POOL_DEVICE_DIR" rpool
for pool in $extra_pools; do
  zpool import -N -d "$POOL_DEVICE_DIR" "$pool"
done
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

create_storage

# Configure networking. Match by name glob so the same image works under any
# qemu device topology (packer's vs. testrole's direct-kernel boot give the NIC
# different kernel names — ens3/ens4/etc.) and on bare metal
# (eno1/enp0s31f6/...). All Predictable Network Interface Names start with
# "en"; only old-style "eth*" is excluded, which requires net.ifnames=0 on
# modern Ubuntu and so is essentially extinct.
#
# Multi-NIC hosts: this stanza claims every "en*" interface as "primary", so
# each onboard NIC will DHCP independently. Bonded / LACP setups need
# bare-metal callers to overwrite this file with an explicit netplan before
# first boot.
#
# Skipped on hetzner: cloud-init's datasource owns networking there, exactly
# as on the stock hcloud image, and a competing static netplan would fight
# cloud-init's generated one.
if [ "$INSTALL_TARGET" != hetzner ]; then
  mkdir -p /mnt/etc/netplan
  cat <<EOF >/mnt/etc/netplan/01-netcfg.yaml
network:
  version: 2
  ethernets:
    primary:
      match:
        name: "en*"
      dhcp4: true
      dhcp-identifier: mac
EOF
fi

run_chroot_script /mnt

# Blank machine-id so systemd regenerates a unique one on first boot —
# otherwise every host from this image shares one (journald, systemd instance
# ids, DHCP DUID).
: >/mnt/etc/machine-id

# Ship the pool cache so the image imports its data pools. Copied after the
# chroot, so the image's first initramfs, built in there, embeds none: Noble's
# initramfs-tools would carry the data pools' entries into early boot.
mkdir -p /mnt/etc/zfs
cp /etc/zfs/zpool.cache /mnt/etc/zfs/zpool.cache

# Only the rpool root dataset itself remains mounted in the host namespace.
zfs unmount "rpool/ROOT/$UBUNTU_NAME"
sync

# Export every pool (zpool_export_retry handles the "pool is busy"
# udev/systemd race; see its comment). Extra pools go first; rpool last
# because the zfs unmount above has already quiesced its root dataset.
for pool in $extra_pools rpool; do
  zpool_export_retry "$pool"
done
