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
#    cert verification on the gitea.lab.fahm.fr ZBM tarball pull.
#  - disable secure boot in firmware setup. rEFInd's EFI binary is
#    signed by the rEFInd project, not Microsoft, so secure-boot-
#    enforcing OEM firmware (locked-down Lenovo / Dell / etc.) will
#    refuse to load it.
set -euxo pipefail

# DISKS, EXTRA_DISKS, LAYOUT, SWAP_SIZE, EXTRA_POOLS, SOURCE_NAME,
# UBUNTU_NAME, UBUNTU_MIRROR come from packer's shell-provisioner env block (see
# qemu.pkr.hcl's variant_config map). Bare-metal callers export them
# by hand before running. EXTRA_DISKS/EXTRA_POOLS are consumed by
# pools.sh; the others by this script and chroot.sh. The
# ZBM_*/REFIND_*/UBUNTU_MIRROR_* vars used downstream are documented
# at the top of chroot.sh.

export DISKS_COUNT
DISKS_COUNT=$(wc -w <<<"$DISKS")

# Placeholder hostname for the shipped image — the deploy step
# (ansible / cloud-init / bare-metal wrapper) is expected to overwrite
# it before first boot. USERNAME is the vagrant user chroot.sh creates
# so packer can SSH back in for the next provisioner stage.
export HOSTNAME=ubuntu
export USERNAME=vagrant

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

# Per-disk partition paths, computed once and exported as space-delimited
# strings so chroot.sh consumes them directly without re-running partdev.
# Partition layout (sgdisk below, numbered in on-disk order): 1 = BIOS
# boot (EF02), 2 = EFI, 3 = swap (single-disk) / metadata vdev (mirror),
# 4 = rpool. rpool is last so a cloud-image deploy can grow it into the
# rest of the disk (chroot.sh's hetzner_growpart grows p4). Each gets a
# GPT name (sgdisk -c: bios/efi/swap|meta/rpool) for readable lsblk/gdisk
# output; consumers resolve by filesystem UUID or device path, never
# by-partlabel (non-unique across the mirror's identically-named disks).
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
PARTITIONS_RPOOL=""
for d in $DISKS; do
  PARTITIONS_EFI+="${PARTITIONS_EFI:+ }$(partdev "$d" 2)"
  if [ "$LAYOUT" = "" ]; then
    PARTITIONS_SWAP+="${PARTITIONS_SWAP:+ }$(partdev "$d" 3)"
  fi
  PARTITIONS_RPOOL+="${PARTITIONS_RPOOL:+ }$(partdev "$d" 4)"
done
export PARTITIONS_EFI PARTITIONS_SWAP PARTITIONS_RPOOL

# Ensure APT doesn't asks questions

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

# Install helpers

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

# Generate /etc/hostid

zgenhostid -f

# Create partitions

for disk in $DISKS; do
  # Defensive wipe -- a no-op against packer's fresh qcow2s, but
  # necessary on bare metal:
  #  - zpool labelclear: ZFS labels at the disk's reserved offsets
  #    (sgdisk --zap-all alone misses those)
  #  - wipefs -a: misc filesystem signatures (ext, mdadm, LUKS headers)
  #  - blkdiscard -f: SSD/sparse hint, frees blocks before partitioning
  #  - sgdisk --zap-all: GPT + protective MBR
  zpool labelclear -f "$disk" || true
  wipefs -a "$disk"
  blkdiscard -f "$disk" || true
  sgdisk --zap-all "$disk"

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
unshare --mount --propagation private arch-chroot /mnt bash </home/vagrant/chroot.sh

# Create the non-rpool ZFS pools requested by the variant (apoc/dozer/
# tank/mouse). Runs while /mnt is still rpool's root so pools.sh can
# copy /etc/zfs/zpool.cache into the shipped install -- without that,
# zfs-import-cache.service on first boot has nothing to import and the
# pools are present on disk but unmounted.
bash /home/vagrant/pools.sh

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
