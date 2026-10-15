#!/bin/bash
# Bootstrap a ZFS-on-root install. Used by packer's qemu build and, from a live
# Ubuntu environment, as the bare-metal copy-paste path for lab-class hosts.
# Two modes:
#  - fresh (default): wipe and partition $DISKS and $EXTRA_DISKS, create every
#    pool, and install rpool/ROOT/$UBUNTU_NAME as the root to boot.
#  - KEEP_POOLS=1: rebuild a host on the disks it has. Nothing is wiped,
#    partitioned, or created but the new root, rpool/ROOT/$UBUNTU_NAME, which
#    lands beside the old one (OLD_ROOT, default rpool's bootfs) and takes its
#    identity and storage wiring. The ESP and firmware entries stay as they are
#    until the boot and refind roles converge on the new root. bootfs stays on
#    the old root, the rollback; the script prints the switch.
#
# Bare-metal fresh installs MUST also:
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
# INSTALL_TARGET, SERIAL_CONSOLE, UBUNTU_NAME, and the optional NEXUS_URL come
# from packer's shell-provisioner env block. Bare-metal callers export them by
# hand. This script consumes the disk/pool vars, derives the UBUNTU_MIRROR*
# URLs and the kernel COMMANDLINE, and passes the exported install vars through
# to chroot.sh, whose environment contract is documented at its top.

# chroot.sh and the role-owned bootstrap files it consumes sit beside this
# script: packer uploads them all to /home/vagrant, and a bare-metal install
# stages them in one directory.
SCRIPTS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
ROLE_FILES=(console-setup keyboard modules_most dracut_host.conf)

fail() {
  echo "${0##*/}: $*" >&2
  exit 1
}

# apt-get update exits 0 even when one component's Packages index fails to
# download (Nexus restart, dropped packet), leaving a partial cache that makes a
# later install fail with a baffling "Unable to locate package". --error-on=any
# turns a failed fetch into a non-zero exit. apt retries each file itself; the
# loop absorbs a mirror outage that outlasts those retries. chroot.sh keeps a
# twin.
apt_update() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if apt-get update --error-on=any; then
      return 0
    fi
    echo "apt-get update attempt ${attempt} failed; retrying in $((attempt * 5))s" >&2
    sleep "$((attempt * 5))"
  done
  echo "apt-get update failed after 5 attempts" >&2
  return 1
}

# Write deb822 apt sources for release $1 from archive mirror $2 and security
# mirror $3, matching both the stock layout and what roles/apt converges to.
# chroot.sh keeps a twin for the new root's sources.
write_sources_list() {
  # Twin of the sources roles/apt/tasks/configure.yml writes, byte for byte
  # apart from the mirror URLs, so a first converge changes only the mirror.
  local deb_arch
  deb_arch=$(dpkg --print-architecture)

  truncate -s0 /etc/apt/sources.list
  mkdir -p /etc/apt/sources.list.d

  cat <<EOF >/etc/apt/sources.list.d/ubuntu.sources
Architectures: $deb_arch
Components: main universe restricted multiverse
Languages: none
X-Repolib-Name: ubuntu
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
Suites: $1 $1-updates $1-backports
Types: deb
URIs: $2
EOF

  cat <<EOF >/etc/apt/sources.list.d/ubuntu-security.sources
Architectures: $deb_arch
Components: main universe restricted multiverse
Languages: none
X-Repolib-Name: ubuntu-security
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
Suites: $1-security
Types: deb
URIs: $3
EOF

  # apt keys /var/lib/apt/lists/ by mirror URL, so changing the mirror
  # orphans the cached indices. The frozen base suite's InRelease is
  # byte-identical whichever mirror serves it (Nexus just proxies upstream),
  # so the next apt-get update records a content "Hit", skips the
  # re-download, then can't open the list file that was never written under
  # the new URL ("can not open …InRelease"). A cloud image's primed indices
  # trip the same way. Drop the cache so each rewrite re-fetches cleanly.
  find /var/lib/apt/lists -type f -delete
}

# Validate every input before anything is written, so a bad layout fails
# before a disk is wiped rather than halfway through. Keep-pools mode checks the
# pools themselves once the tools that read them are installed
# (check_kept_pools).
preflight() {
  local disk device pool key file var extra_disks_needed=0
  local -a disks extra_disks
  local -A seen_disks=()

  : "${UBUNTU_NAME:?}"

  # The setup below rewrites the running system's hostid and mdadm policy
  # before touching a disk, which an installed host must never see.
  [ "$(findmnt -no FSTYPE /)" != zfs ] ||
    fail "the running root is on ZFS; provision.sh runs from a live environment or a build VM, never an installed host"

  [[ $INSTALL_TARGET =~ ^(bare_metal|qemu|hetzner)$ ]] ||
    fail "INSTALL_TARGET must be bare_metal, qemu, or hetzner (got '$INSTALL_TARGET')"
  [ "$INSTALL_TARGET" = bare_metal ] || [ -n "${SERIAL_CONSOLE:-}" ] ||
    fail "INSTALL_TARGET=$INSTALL_TARGET requires SERIAL_CONSOLE"
  [ "${TARGET_HOSTNAME+set}" = "${TARGET_USERNAME+set}" ] ||
    fail "TARGET_HOSTNAME and TARGET_USERNAME must be set together"
  [[ $UBUNTU_NAME =~ ^[a-z]+$ ]] || fail "UBUNTU_NAME must be a release codename (got '$UBUNTU_NAME')"
  [[ $HOSTNAME =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || fail "invalid TARGET_HOSTNAME '$HOSTNAME'"
  [[ $USERNAME =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail "invalid TARGET_USERNAME '$USERNAME'"
  # The login user has no password and sshd refuses passwords, so every key in
  # SSH_KEY_PUB must parse. The Hetzner image creates no login user; cloud-init
  # does on first boot.
  if [ "$INSTALL_TARGET" != hetzner ]; then
    [ -n "${SSH_KEY_PUB:-}" ] || fail "SSH_KEY_PUB is required"
    while IFS= read -r key; do
      if [ -n "$key" ]; then
        ssh-keygen -l -f /dev/stdin <<<"$key" >/dev/null || fail "SSH_KEY_PUB holds an unusable key: $key"
      fi
    done <<<"$SSH_KEY_PUB"
  fi

  for file in chroot.sh "${ROLE_FILES[@]}"; do
    [ -f "$SCRIPTS_DIR/$file" ] || fail "required install file not found: $SCRIPTS_DIR/$file"
  done
  case $INSTALL_TARGET in
  qemu) [ -f "$SCRIPTS_DIR/homelab_guest_journal.service" ] || fail "homelab_guest_journal.service not found in $SCRIPTS_DIR" ;;
  hetzner) [ -d "$SCRIPTS_DIR/hetzner" ] || fail "hetzner/ not found in $SCRIPTS_DIR" ;;
  esac

  if [ "$KEEP_POOLS" = 1 ]; then
    # A rebuild keeps every disk as it is; a layout input would only suggest
    # otherwise.
    for var in DISKS EXTRA_DISKS LAYOUT SWAP_SIZE PODMAN_SIZE META_SIZE EXTRA_POOLS; do
      [ -z "${!var+set}" ] || fail "KEEP_POOLS=1 keeps the disks and pools as they are; unset $var"
    done
    [ "$INSTALL_TARGET" != hetzner ] || fail "KEEP_POOLS=1 rebuilds a bare_metal host or a qemu fixture, not a hetzner image"
    # The rebuilt root is a real host: its name and login user are not the
    # image placeholders.
    [ -n "${TARGET_HOSTNAME:-}" ] || fail "KEEP_POOLS=1 requires TARGET_HOSTNAME and TARGET_USERNAME"
    [ -z "${OLD_ROOT:-}" ] || [[ $OLD_ROOT =~ ^rpool/ROOT/[^/]+$ ]] ||
      fail "OLD_ROOT must name a root under rpool/ROOT (got '$OLD_ROOT')"
    return 0
  fi

  # Optional layout features may be empty but must be set explicitly.
  : "${EXTRA_DISKS?}" "${LAYOUT?}" "${PODMAN_SIZE?}" "${META_SIZE?}" "${EXTRA_POOLS?}"
  : "${DISKS:?}" "${SWAP_SIZE:?}" "${ZBM_VERSION:?}"

  read -r -a disks <<<"$DISKS"
  read -r -a extra_disks <<<"$EXTRA_DISKS"
  case $LAYOUT in
  "") ((${#disks[@]} == 1)) || fail "a single-disk LAYOUT takes one disk in DISKS (got ${#disks[@]})" ;;
  mirror) ((${#disks[@]} >= 2)) || fail "LAYOUT=mirror takes at least two disks in DISKS (got ${#disks[@]})" ;;
  *) fail "LAYOUT must be empty or mirror (got '$LAYOUT')" ;;
  esac
  for pool in $EXTRA_POOLS; do
    case $pool in
    dozer) extra_disks_needed=$((extra_disks_needed + 2)) ;;
    tank_mouse) extra_disks_needed=$((extra_disks_needed + 4)) ;;
    *) fail "unknown EXTRA_POOLS entry '$pool'" ;;
    esac
  done
  ((${#extra_disks[@]} == extra_disks_needed)) ||
    fail "EXTRA_POOLS '$EXTRA_POOLS' takes $extra_disks_needed disks in EXTRA_DISKS (got ${#extra_disks[@]})"

  # Compare resolved devices, so a /dev/disk/by-id alias of a listed disk is a
  # duplicate too. A disk in use -- a mounted filesystem, active swap, or an
  # LVM or dm-crypt device on it -- is never a target.
  for disk in "${disks[@]}" "${extra_disks[@]}"; do
    [[ $disk == /dev/* ]] || fail "disk path must start with /dev/ (got '$disk')"
    device=$(readlink -f -- "$disk")
    [ -b "$device" ] && [ "$(lsblk -dno TYPE "$device")" = disk ] || fail "$disk is not a whole disk"
    [[ ! -v seen_disks[$device] ]] || fail "disk '$disk' is listed more than once"
    seen_disks[$device]=1
    [ -z "$(lsblk -nro MOUNTPOINT "$device" | tr -d '\n')" ] || fail "$disk has a mounted filesystem or active swap"
    if lsblk -nro TYPE "$device" | awk '$1 == "lvm" || $1 == "crypt" { found = 1 } END { exit !found }'; then
      fail "$disk backs an LVM or dm-crypt device"
    fi
  done
}

# Packer selects qemu or hetzner; direct callers get the bare-metal path.
export INSTALL_TARGET="${INSTALL_TARGET:-bare_metal}"

# Never inferred from what the disks hold: a fresh install wipes them.
export KEEP_POOLS="${KEEP_POOLS:-}"
[[ $KEEP_POOLS =~ ^1?$ ]] || fail "KEEP_POOLS must be empty or 1 (got '$KEEP_POOLS')"

# Placeholder hostname for the shipped image — the deploy step
# (ansible / cloud-init / bare-metal wrapper) is expected to overwrite
# it before first boot. USERNAME is the vagrant user chroot.sh creates
# so packer can SSH back in for the next provisioner stage. Bare-metal
# callers override both via TARGET_HOSTNAME / TARGET_USERNAME (distinct
# names: the live-USB shell already sets HOSTNAME) so the installed
# system boots as the real host with the real operator user and no
# vagrant user is ever created. Only an unset override takes the
# placeholder; preflight rejects an empty one.
export HOSTNAME="${TARGET_HOSTNAME-ubuntu}"
export USERNAME="${TARGET_USERNAME-vagrant}"

preflight

# The install pulls through Nexus when NEXUS_URL names its host (empty or unset
# means upstream, as with group_vars' nexus_url); the installed system always
# ships the upstream pair.
case $(uname -m) in
x86_64)
  UBUNTU_MIRROR_UPSTREAM=http://archive.ubuntu.com/ubuntu
  UBUNTU_MIRROR_SECURITY_UPSTREAM=http://security.ubuntu.com/ubuntu
  nexus_repositories=(ubuntu-archive ubuntu-security)
  ;;
aarch64)
  UBUNTU_MIRROR_UPSTREAM=http://ports.ubuntu.com/ubuntu-ports
  UBUNTU_MIRROR_SECURITY_UPSTREAM=$UBUNTU_MIRROR_UPSTREAM
  nexus_repositories=(ubuntu-ports ubuntu-ports)
  ;;
*) fail "unsupported architecture $(uname -m)" ;;
esac
if [ -n "${NEXUS_URL:-}" ]; then
  UBUNTU_MIRROR=http://$NEXUS_URL/repository/${nexus_repositories[0]}
  UBUNTU_MIRROR_SECURITY=http://$NEXUS_URL/repository/${nexus_repositories[1]}
else
  UBUNTU_MIRROR=$UBUNTU_MIRROR_UPSTREAM
  UBUNTU_MIRROR_SECURITY=$UBUNTU_MIRROR_SECURITY_UPSTREAM
fi
export UBUNTU_MIRROR UBUNTU_MIRROR_SECURITY UBUNTU_MIRROR_UPSTREAM UBUNTU_MIRROR_SECURITY_UPSTREAM

# The kernel command line ZBM boots the new roots with: the
# org.zfsbootmenu:commandline property on rpool/ROOT, and the rEFInd menu's
# options, which chroot.sh writes. Every target keeps VGA output. QEMU fixtures
# add SERIAL_CONSOLE, the serial console verify-boot reads, and disable
# mitigations in disposable nested-KVM cells; Hetzner lists its SERIAL_CONSOLE
# before tty0, so the VGA console keeps /dev/console. Bare-metal lab and pug
# have no UART. The boot role owns the final prod command line.
case $INSTALL_TARGET in
qemu) COMMANDLINE="console=tty0 $SERIAL_CONSOLE mitigations=off" ;;
hetzner) COMMANDLINE="$SERIAL_CONSOLE console=tty0" ;;
*) COMMANDLINE="console=tty0" ;;
esac
export COMMANDLINE

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

# Fresh mode: wipe and partition the disks and create every pool on them,
# leaving the pools exported and extra_pools naming the data pools.
create_pools() {
  # ZFSBootMenu boots every root with its own hostid (spl.spl_hostid), which
  # for our host-agnostic build is upstream's default. Creating the pools under
  # it and giving the root the same /etc/hostid keeps the labels, the running
  # kernel, and the root's initramfs in agreement, as upstream's guides do.
  zgenhostid -f 0x00bab10c

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
  # so the shell word-splits them into the zpool args. Hetzner's rpool gets
  # the autoexpand=on that fox's zfs_pool_properties assert.
  local autoexpand=off
  [ "$INSTALL_TARGET" != hetzner ] || autoexpand=on

  # shellcheck disable=SC2086
  zpool create -f \
    -o ashift=12 \
    -o autoexpand="$autoexpand" \
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

  # Every root under rpool/ROOT inherits the command line.
  zfs create -o canmount=off -o mountpoint=none rpool/ROOT
  zfs set org.zfsbootmenu:commandline="$COMMANDLINE" rpool/ROOT

  create_extra_pools

  # zpool create recorded the kernel device paths it was given; export every
  # pool for the import from POOL_DEVICE_DIR below.
  extra_pools=$(zpool list -H -o name | grep -vx rpool || true)
  for pool in $extra_pools rpool; do
    zpool_export_retry "$pool"
  done
}

# Keep-pools mode: check the pools and the old root before anything on the
# disks changes, and stage the old root's identity in kept_identity. Every pool
# is imported read-only, which writes nothing to the disks, and with -f: the
# old root last imported them under a hostid the live environment lacks. Leaves
# the pools exported, extra_pools naming the data pools, and the old root's
# /etc/hostid as the live environment's, for the read-write import that
# follows to record in the pool labels.
check_kept_pools() {
  local available user_ids
  old_root_dir=$(mktemp -d)
  # A failed check leaves the disks as they were found.
  trap 'umount "$old_root_dir" 2>/dev/null || true; zpool export -a' EXIT

  zpool import -a -N -f -o readonly=on -R /mnt -d "$POOL_DEVICE_DIR"
  zpool list -H rpool >/dev/null || fail "no rpool found in $POOL_DEVICE_DIR"
  extra_pools=$(zpool list -H -o name | grep -vx rpool || true)
  [ "$(zpool status -x)" = "all pools are healthy" ] || fail "a pool is unhealthy; see zpool status -x"

  OLD_ROOT=${OLD_ROOT:-$(zpool get -H -o value bootfs rpool)}
  if ! [[ $OLD_ROOT =~ ^rpool/ROOT/[^/]+$ ]] || ! zfs list -H "$OLD_ROOT" >/dev/null; then
    fail "the old root '$OLD_ROOT' is not a root under rpool/ROOT"
  fi
  [ "$OLD_ROOT" != "rpool/ROOT/$UBUNTU_NAME" ] ||
    fail "$OLD_ROOT is the old root; a rebuild installs another release beside it"
  if zfs list -H "rpool/ROOT/$UBUNTU_NAME" >/dev/null 2>&1; then
    fail "rpool/ROOT/$UBUNTU_NAME already exists; destroy it (zfs destroy -r rpool/ROOT/$UBUNTU_NAME) to reinstall, as after a failed install"
  fi
  # The new root lands on rpool beside the old one and the service datasets,
  # which must not run out of space under it.
  available=$(zfs get -Hp -o value available rpool)
  ((available >= 20 * 1024 ** 3)) ||
    fail "rpool has $((available / 1024 ** 3)) GiB available; the install needs 20"

  # zfsutil: the old root's mountpoint is /, not legacy.
  mount -t zfs -o ro,zfsutil "$OLD_ROOT" "$old_root_dir"
  for file in hostid machine-id fstab; do
    [ -s "$old_root_dir/etc/$file" ] || fail "$OLD_ROOT has no /etc/$file"
  done
  compgen -G "$old_root_dir/etc/ssh/ssh_host_*_key" >/dev/null || fail "$OLD_ROOT has no SSH host keys"

  # An existing login user keeps its ids: it owns files on the kept datasets.
  # chroot.sh creates the user with them.
  if user_ids=$(awk -F: -v user="$USERNAME" '$1 == user { print $3, $4; found = 1 } END { exit !found }' "$old_root_dir/etc/passwd"); then
    read -r USER_UID USER_GID <<<"$user_ids"
    [ "$(awk -F: -v gid="$USER_GID" '$3 == gid { print $1 }' "$old_root_dir/etc/group")" = "$USERNAME" ] ||
      fail "$USERNAME's primary group in $OLD_ROOT is not $USERNAME; chroot.sh creates the user with its own group"
    export USER_UID USER_GID
  fi

  # The identity and storage wiring the new root takes over. The fresh path
  # generates these instead: zgenhostid, a blank machine-id, the netplan
  # stanza, and create_storage's fstab and mdadm.conf.
  kept_identity=$(mktemp -d)
  mkdir -p "$kept_identity"/etc/{ssh,mdadm}
  cp -a "$old_root_dir"/etc/{hostid,machine-id,fstab} "$kept_identity/etc/"
  cp -a "$old_root_dir"/etc/ssh/ssh_host_* "$kept_identity/etc/ssh/"
  if [ -d "$old_root_dir/etc/netplan" ]; then
    cp -a "$old_root_dir/etc/netplan" "$kept_identity/etc/"
  fi
  if [ -f "$old_root_dir/etc/mdadm/mdadm.conf" ]; then
    cp -a "$old_root_dir/etc/mdadm/mdadm.conf" "$kept_identity/etc/mdadm/"
  fi

  umount "$old_root_dir"
  zpool export -a
  trap - EXIT
  cp "$kept_identity/etc/hostid" /etc/hostid
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
for d in ${DISKS:-}; do
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

# A pool already imported in the live environment is a mistake: a fresh
# install would wipe its disks or collide with its name, and a rebuild imports
# every pool itself, under the old root's hostid. Checked once the tools above
# can list pools, before the environment's storage policy changes.
imported_pools=$(zpool list -H -o name)
[ -z "$imported_pools" ] ||
  fail "export the pools imported in the live environment first: $imported_pools"

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

# Stop udev's `mdadm --incremental` from assembling arrays off the target
# disks: off a stale superblock while a fresh install rewrites the tables, or
# off the kept arrays a rebuild leaves to the new root. Declarative, so it
# cannot leak the way a stop-exec-queue trap can if the script exits
# mid-partition. Live environment only -- the new root gets its own mdadm.conf.
printf 'AUTO -all\n' >/etc/mdadm/mdadm.conf

if [ "$KEEP_POOLS" = 1 ]; then
  check_kept_pools
else
  create_pools
fi

# Import every pool from POOL_DEVICE_DIR so zpool.cache records stable names.
# rpool comes back with a temporary mountpoint of /mnt. An altroot import
# implies cachefile=none, so name the cache explicitly: the shipped zpool.cache
# then lists rpool alongside the data pools.
#
# A rebuild imports unforced: the live environment now has the old root's
# /etc/hostid, which matches the hostid ZFSBootMenu boots every root with
# (0x00bab10c), so ZFS still refuses pools another running system holds.
zpool import -N -R /mnt -o cachefile=/etc/zfs/zpool.cache -d "$POOL_DEVICE_DIR" rpool
for pool in $extra_pools; do
  zpool import -N -d "$POOL_DEVICE_DIR" "$pool"
done

# The new root stays at mountpoint=legacy until the install completes: ZBM
# offers only roots mounted at /, so a failed install never shows up as
# bootable. canmount=noauto keeps any other root from mounting it. A rebuild
# leaves bootfs on the old root.
zfs create -o canmount=noauto -o mountpoint=legacy "rpool/ROOT/$UBUNTU_NAME"
if [ "$KEEP_POOLS" != 1 ]; then
  zpool set "bootfs=rpool/ROOT/$UBUNTU_NAME" rpool
fi
mount -t zfs "rpool/ROOT/$UBUNTU_NAME" /mnt

# Wait for udev to wire the new device nodes before the install uses them.
udevadm settle

# If a fetch fails, --verbose surfaces each retrieve/validate step live and the
# handler dumps debootstrap's own log -- which otherwise dies with the build VM
# -- so the next occurrence stays diagnosable. A debootstrap older than the
# release lacks its script; every Ubuntu release since Gutsy shares that one.
debootstrap_script=()
if [ ! -e "/usr/share/debootstrap/scripts/$UBUNTU_NAME" ]; then
  debootstrap_script=(/usr/share/debootstrap/scripts/gutsy)
fi
debootstrap --verbose "$UBUNTU_NAME" /mnt "$UBUNTU_MIRROR" "${debootstrap_script[@]}" || {
  rc=$?
  echo "=== debootstrap failed (exit $rc); /mnt/debootstrap/debootstrap.log tail ===" >&2
  tail -n 300 /mnt/debootstrap/debootstrap.log >&2 || echo "(no debootstrap.log present)" >&2
  exit "$rc"
}

if [ "$KEEP_POOLS" = 1 ]; then
  # The old root's identity and storage wiring, in place before chroot.sh
  # installs the packages that read it: the initramfs embeds hostid and
  # mdadm.conf, openssh-server keeps existing host keys, mdadm an existing
  # mdadm.conf, and systemd an existing machine-id.
  cp -a "$kept_identity/etc" /mnt/
else
  # /etc/hostid must match the one ZFS saw at pool creation.
  cp /etc/hostid /mnt/etc
  create_storage
fi

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
# cloud-init's generated one. A rebuild keeps the old root's netplan.
if [ "$INSTALL_TARGET" != hetzner ] && [ "$KEEP_POOLS" != 1 ]; then
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

# Stage chroot.sh and everything it installs in one directory inside the new
# root, passed to it as CHROOT_FILES. Under /var/tmp, not /tmp: arch-chroot
# shadows the chroot's /tmp with a private tmpfs.
chroot_files=/var/tmp/homelab_install
rm -rf -- "/mnt${chroot_files:?}"
mkdir -p "/mnt$chroot_files"
cp "$SCRIPTS_DIR/chroot.sh" "/mnt$chroot_files/"
for role_file in "${ROLE_FILES[@]}"; do
  cp "$SCRIPTS_DIR/$role_file" "/mnt$chroot_files/"
done
case $INSTALL_TARGET in
qemu) cp "$SCRIPTS_DIR/homelab_guest_journal.service" "/mnt$chroot_files/" ;;
hetzner) cp -a "$SCRIPTS_DIR/hetzner" "/mnt$chroot_files/" ;;
esac

# dpkg fsyncs every unpacked file by default, which dominates the install's
# wall-clock on network-backed disks (EBS on the AWS bake) and still costs
# plenty locally. A disposable image or fixture that crashes mid-install is
# rebuilt, never booted, so per-file durability buys nothing there. A real host
# keeps it: an interrupted install is costlier to redo, and the local-SSD speed
# win is small.
if [ "$INSTALL_TARGET" != bare_metal ]; then
  echo force-unsafe-io >/mnt/etc/dpkg/dpkg.cfg.d/90-build-unsafe-io
fi

# arch-chroot bind-mounts the API filesystems and /etc/resolv.conf for the
# chroot's lifetime, so apt resolves names without the build host's DNS landing
# in the image. It mounts into the caller's namespace, though, and a mount
# chroot.sh adds itself (/boot/efi) would outlive it and block the unmount of
# /mnt below; the private mount namespace takes every one of them down on exit.
# The exported install variables flow straight through.
CHROOT_FILES=$chroot_files unshare --mount --propagation private \
  arch-chroot /mnt bash "$chroot_files/chroot.sh" </dev/null

rm -f /mnt/etc/dpkg/dpkg.cfg.d/90-build-unsafe-io
rm -rf -- "/mnt${chroot_files:?}"

# Blank machine-id so systemd regenerates a unique one on first boot —
# otherwise every host from this image shares one (journald, systemd instance
# ids, DHCP DUID). A rebuild keeps the old root's.
if [ "$KEEP_POOLS" != 1 ]; then
  : >/mnt/etc/machine-id
fi

# Ship the pool cache so the image imports its data pools. Copied after the
# chroot, so the image's first initramfs, built in there, embeds none: Noble's
# initramfs-tools would carry the data pools' entries into early boot.
mkdir -p /mnt/etc/zfs
cp /etc/zfs/zpool.cache /mnt/etc/zfs/zpool.cache

# Commit point: the root becomes bootable. Unmounted first, since a mountpoint
# change remounts a mounted dataset at its new mountpoint.
umount /mnt
zfs set mountpoint=/ "rpool/ROOT/$UBUNTU_NAME"
sync

# Export every pool (zpool_export_retry handles the "pool is busy"
# udev/systemd race; see its comment). Extra pools go first; rpool last
# because the umount above has already quiesced its root dataset.
for pool in $extra_pools rpool; do
  zpool_export_retry "$pool"
done

if [ "$KEEP_POOLS" = 1 ]; then
  set +x
  cat <<EOF

rpool/ROOT/$UBUNTU_NAME is installed beside $OLD_ROOT, which stays the root to
boot. To boot the new root, from this live environment:
  zpool import -N rpool && zpool set bootfs=rpool/ROOT/$UBUNTU_NAME rpool && zpool export rpool
or from $OLD_ROOT:
  zpool set bootfs=rpool/ROOT/$UBUNTU_NAME rpool && systemctl reboot
To roll back afterwards:
  zpool set bootfs=$OLD_ROOT rpool && systemctl reboot
EOF
fi
