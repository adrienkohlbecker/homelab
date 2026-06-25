#!/bin/bash
# Bootstrap a ZFS-on-root install onto $DISKS. Used by packer's qemu
# build and as the bare-metal copy-paste path for provisioning new
# lab-class hosts.
#
# Bare-metal callers MUST also:
#  - pre-flight $DISKS. Every entry is wiped unconditionally (sgdisk
#    --zap-all + wipefs + blkdiscard + zpool labelclear); a wrong
#    device path destroys data in seconds.
#  - rotate /home/vagrant/.ssh/authorized_keys (which currently holds
#    the publicly-known vagrant insecure pubkey) and remove
#    /etc/sudoers.d/vagrant before the host gets a routable IP. The
#    shipped image is otherwise a free root shell on any lab LAN.
#  - on mirror-rpool variants, supply matching-size disks. The
#    rpool mirror caps at the smallest disk's partition 3, so a
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

# DISKS, EXTRA_DISKS, LAYOUT, SWAP_SIZE, EXTRA_POOLS, SOURCE_NAME,
# IMAGE_TARGET, QEMU_TEST_IMAGE, UBUNTU_NAME, and UBUNTU_MIRROR come from
# packer's shell-provisioner env block. Bare-metal callers export them by hand.
# This script consumes the disk/pool vars and passes the exported install vars
# through to chroot.sh. The ZBM_*/REFIND_*/UBUNTU_MIRROR_* vars used downstream
# are documented at the top of chroot.sh.

export DISKS_COUNT
DISKS_COUNT=$(wc -w <<<"$DISKS")

# Placeholder hostname for the shipped image — the deploy step
# (ansible / cloud-init / bare-metal wrapper) is expected to overwrite
# it before first boot. USERNAME is the vagrant user chroot.sh creates
# so packer can SSH back in for the next provisioner stage.
export HOSTNAME=ubuntu
export USERNAME=vagrant

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
# device node (matches upstream openzfs/zfs#16036) — notably the mirror
# variants' rpool/swap zvol, whose /dev/zvol/rpool/swap node trips the
# very next `zpool export`. `zpool export -f` doesn't bypass the
# spa_refcount EBUSY gate, so force is pointless. udevadm settle drains
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
    zpool create -f -o autotrim=on "${EXTRA_ZPOOL_OPTS[@]}" \
      tank raidz2 "$(partdev "$tm1" 1)" "$(partdev "$tm2" 1)" "$tank3" "$tank4"
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
# Partition layout (sgdisk below): 1 = BIOS boot (EF02), 2 = EFI, 3 = swap
# (single-disk) / metadata vdev (mirror), 5 = podman store (optional, when
# PODMAN_SIZE is set), 4 = rpool. rpool keeps number 4 and is carved last
# (-n4:0:0) so it grows into the rest of the disk regardless of whether the
# podman partition is present -- a cloud-image deploy still grows p4
# (chroot.sh's hetzner_growpart). The podman partition is numbered 5 but sits
# physically before rpool. Each gets a GPT name (sgdisk -c:
# bios/efi/swap|meta/podman/rpool) for readable lsblk/gdisk output; consumers
# resolve by filesystem UUID or device path, never by-partlabel (non-unique
# across the mirror's identically-named disks) -- except the single-disk
# podman partition, where by-partlabel IS unique (one disk).
#
# Swap is the disk-backed *overflow* behind zram, which the swap role
# runs as the primary high-priority device (notes/swap_strategy.md),
# sized by SWAP_SIZE (per-variant in qemu.pkr.hcl):
#   - single-disk: a dedicated 8200 partition (p3) — a real partition is
#     deadlock-free, unlike swap on a zvol.
#   - mirror: drops the swap partition and uses an rpool zvol (created
#     below). The mirror rpool already gives the redundancy the old
#     per-disk swap + mdadm raid0 stripe faked, without the failure mode
#     where one dead disk took the whole stripe (and its swapped-out
#     pages) down; zram-primary keeps the zvol cold so its residual
#     deadlock risk is only reached in true exhaustion.
# The metadata-vdev slot (partition 3, mirror variants only) is reserved;
# nothing consumes it yet (see notes/special-vdev-sizing.md).
PARTITIONS_EFI=""
PARTITIONS_SWAP=""
PARTITIONS_PODMAN=""
PARTITIONS_RPOOL=""
for d in $DISKS; do
  PARTITIONS_EFI+="${PARTITIONS_EFI:+ }$(partdev "$d" 2)"
  if [ "$LAYOUT" = "" ]; then
    PARTITIONS_SWAP+="${PARTITIONS_SWAP:+ }$(partdev "$d" 3)"
  fi
  if [ -n "${PODMAN_SIZE:-}" ]; then
    PARTITIONS_PODMAN+="${PARTITIONS_PODMAN:+ }$(partdev "$d" 5)"
  fi
  PARTITIONS_RPOOL+="${PARTITIONS_RPOOL:+ }$(partdev "$d" 4)"
done
export PARTITIONS_EFI PARTITIONS_SWAP PARTITIONS_PODMAN PARTITIONS_RPOOL

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
apt_update
apt-get install --yes arch-install-scripts debootstrap gdisk zfsutils-linux

zgenhostid -f

for disk in $DISKS; do
  # Defensive wipe -- a no-op against packer's fresh qcow2s, but
  # necessary on bare metal:
  #  - zpool labelclear: ZFS labels at the disk's reserved offsets
  #    (sgdisk --zap-all alone misses those)
  #  - wipefs -a: misc filesystem signatures (ext, mdadm, LUKS headers)
  #  - blkdiscard -f: SSD/sparse hint, frees blocks before partitioning
  #  - sgdisk --zap-all: GPT + protective MBR
  wipe_disk "$disk"

  sgdisk -a1 -n1:24K:+1000K -t1:EF02 -c1:bios "$disk" # MBR booting (EF02 = BIOS boot partition)
  sgdisk -n2:1M:+1G -t2:EF00 -c2:efi "$disk"          # EFI (EF00 = EFI system partition)

  # Single-disk swap partition, sized by SWAP_SIZE. Mirror variants swap
  # on the rpool zvol below instead (see notes/swap_strategy.md).
  if [ "$LAYOUT" = "" ]; then
    sgdisk "-n3:0:+$SWAP_SIZE" -t3:8200 -c3:swap "$disk" # Swap (8200 = Linux Swap)
  fi

  if [ "$LAYOUT" = "mirror" ]; then
    sgdisk -n3:0:+2G -t3:BF01 -c3:meta "$disk" # metadata vdev (BF01 = Solaris /usr & Mac ZFS, default when doing zpool create)
  fi

  # Dedicated podman store partition (number 5, carved before rpool so rpool
  # keeps number 4 and still grows to the end of the disk). Single-disk hosts
  # carry a plain ext4 here; mirror hosts mdadm the per-disk p5s into a raid5
  # (chroot.sh). 8300 = Linux filesystem.
  if [ -n "${PODMAN_SIZE:-}" ]; then
    sgdisk "-n5:0:+$PODMAN_SIZE" -t5:8300 -c5:podman "$disk"
  fi
  sgdisk -n4:0:0 -t4:BF00 -c4:rpool "$disk" # rpool (BF00 = Solaris root)

  sgdisk -p "$disk"
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

# Mirror swap lands on this zvol instead of a per-disk swap partition +
# mdadm raid0; mirror rpool already provides redundancy, and the swap
# role can grow it to the per-host size later. Options follow OpenZFS
# swap-zvol guidance:
#   -b $(getconf PAGESIZE): volblocksize = kernel page size so one
#     page-out is one ZFS block (4K on x86_64 / arm64 4K-pages kernel).
#     zfs warns this is below its 8K default minimum, but that minimum
#     guards against raidz parity/padding waste — rpool is a mirror, so
#     there's none, and 8K would force a read-modify-write per page-out.
#     Keep 4K; the warning is benign here.
#   compression=zle: cheap zero-page squash; pages are already-compressed
#     memory, so general compression would just burn CPU.
#   logbias=throughput + sync=always: every write hits stable storage
#     (otherwise an OOM panic could lose anonymous pages).
#   primarycache=metadata + secondarycache=none: don't let ARC/L2ARC
#     re-cache pages the kernel is explicitly evicting.
#   com.sun:auto-snapshot=false: snapshotting swap pins the working set
#     forever for no benefit.
if [ "$LAYOUT" = "mirror" ]; then
  zfs create \
    -V "$SWAP_SIZE" \
    -b "$(getconf PAGESIZE)" \
    -o compression=zle \
    -o logbias=throughput \
    -o sync=always \
    -o primarycache=metadata \
    -o secondarycache=none \
    -o com.sun:auto-snapshot=false \
    rpool/swap
fi

zpool set "bootfs=rpool/ROOT/$UBUNTU_NAME" rpool

# Export, then re-import with a temporary mountpoint of /mnt. The
# export races udev's probe of the just-created rpool/swap zvol on
# mirror variants — zpool_export_retry settles first (see its comment).

zpool_export_retry rpool
zpool import -N -R /mnt rpool
zfs mount "rpool/ROOT/$UBUNTU_NAME"

# Wait for zvol udev symlinks (notably /dev/zvol/rpool/swap on mirror
# variants) before arch-chroot runs — `zfs create -V` returns before
# udev finishes wiring the device, and chroot.sh's mkswap would race
# the udev create.
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

# Build-time dpkg I/O mode: dpkg fsyncs every unpacked file by default,
# which dominates the chroot install's wall-clock on network-backed disks
# (EBS on the AWS bake) and still costs plenty locally. A build that
# crashes mid-install is rebuilt, never booted, so per-file durability buys
# nothing — and the shipped image is quiesced by the zpool export below
# regardless. Removed before the image is sealed.
echo force-unsafe-io >/mnt/etc/dpkg/dpkg.cfg.d/90-build-unsafe-io

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

rm /mnt/etc/dpkg/dpkg.cfg.d/90-build-unsafe-io

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
