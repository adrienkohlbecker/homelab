#!/bin/bash
# Bootstrap a ZFS-on-root install onto $DISKS. Used by packer's qemu
# build and as the bare-metal copy-paste path for provisioning new
# lab-class hosts.
#
# Bare-metal callers MUST rotate /home/vagrant/.ssh/authorized_keys
# (which currently holds the publicly-known vagrant insecure pubkey)
# and remove /etc/sudoers.d/vagrant before the host gets a routable
# IP. The shipped image is otherwise a free root shell on any lab LAN.
set -euxo pipefail

case $SOURCE_NAME in
ubuntu-zfs)
  DISKS=(/dev/vdb)
  export LAYOUT=""
  ;;
ubuntu-zfs-lab)
  DISKS=(/dev/vdb /dev/vdc /dev/vdd)
  export LAYOUT="mirror"
  ;;
*)
  echo >&2 "Unknown build $SOURCE_NAME"
  exit 1
  ;;
esac

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

# Install helpers

apt-get update
apt-get install --yes arch-install-scripts debootstrap gdisk zfsutils-linux

# Generate /etc/hostid

zgenhostid -f

# Create partitions

for disk in "${DISKS[@]}"; do
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

  sgdisk -n1:1M:+512M -t1:EF00 "$disk"       # EFI (EF00 = EFI system partition)
  sgdisk -a1 -n4:24K:+1000K -t4:EF02 "$disk" # MBR booting (EF02 = BIOS boot partition)

  if [ "$LAYOUT" = "" ]; then
    sgdisk -n2:0:+4G -t2:8200 "$disk" # Swap (8200 = Linux Swap)
  else
    sgdisk -n2:0:+4G -t2:FD00 "$disk" # Swap (FD00 = Linux RAID)
  fi

  if [ "$SOURCE_NAME" = "ubuntu-zfs-lab" ]; then
    sgdisk -n5:-2G:0 -t5:BF01 "$disk" # metadata vdev (BF01 = Solaris /usr & Mac ZFS, default when doing zpool create)
  fi
  sgdisk -n3:0:0 -t3:BF00 "$disk" # rpool (BF00 = Solaris root)

  sgdisk -p "$disk"
done

# Wait for udev to expose every new partition node (/dev/vdbN, ...)
# before zpool create reads them. Replaces a per-iteration `sync; sleep 2`
# pair that was timing-based cargo for the same goal.
udevadm settle

# Create the zpool

rpool_devs=()
for d in "${DISKS[@]}"; do rpool_devs+=("$(partdev "$d" 3)"); done

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
  rpool $LAYOUT "${rpool_devs[@]}"

# Create initial file systems

zfs create -o canmount=off -o mountpoint=none rpool/ROOT
zfs create -o canmount=noauto -o mountpoint=/ "rpool/ROOT/$UBUNTU_NAME"

zpool set "bootfs=rpool/ROOT/$UBUNTU_NAME" rpool

# Export, then re-import with a temporary mountpoint of /mnt

zpool export rpool
zpool import -N -R /mnt rpool
zfs mount "rpool/ROOT/$UBUNTU_NAME"

# Verify that everything is mounted correctly

mount | grep mnt

# Install Ubuntu

debootstrap "$UBUNTU_NAME" /mnt "$UBUNTU_MIRROR"

# Copy files into the new install. /etc/hostid must match the one ZFS
# saw at pool creation; arch-chroot bind-mounts /etc/resolv.conf so apt
# inside the chroot can resolve hostnames, and the bind goes away when
# arch-chroot exits — the shipped image keeps whatever debootstrap put
# there (empty), not the build host's DNS settings.

cp /etc/hostid /mnt/etc

# Configure networking. Match by name glob so the same image works
# under any qemu device topology (packer's vs. testrole's direct-kernel
# boot give the NIC different kernel names — ens3/ens4/etc.) and on
# baremetal (eno1/enp0s31f6/...). All Predictable Network Interface
# Names start with "en"; only old-style "eth*" is excluded, which
# requires net.ifnames=0 on modern Ubuntu and so is essentially extinct.
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

# Chroot into the new OS via arch-chroot (arch-install-scripts). It sets
# up proc/sys/dev/devpts/run/efivarfs in a private mount namespace and
# bind-mounts /etc/resolv.conf for the chroot's lifetime, so apt can
# resolve hostnames during the install without leaving the build host's
# DNS pinned in the shipped image.
#
# Env propagation: arch-chroot inherits the calling shell's env, so
# packer's UBUNTU_*/ZBM_*/REFIND_NAME/SSH_KEY_PUB (already exported via
# the shell provisioner env block) flow straight through. Script-local
# vars must be exported explicitly. DISKS is a bash array, and bash
# silently refuses to put array-typed variables in env even after a
# scalar reassignment — flatten under a distinct name; chroot.sh
# re-parses with `read -r -a DISKS <<<"$DISKS_LIST"`. New vars added
# later need only be exported here, not enumerated on the chroot line.
export DISKS_LIST="${DISKS[*]}"
arch-chroot /mnt bash </home/vagrant/chroot.sh

# Copy the on-pool kernel + initrd (and the matching ZBM-style cmdline) out
# to the build VM filesystem so packer's file provisioner can download them
# alongside the qcow2. Test harness uses these to direct-boot the variant on
# arches where the rEFInd -> ZBM -> kexec chain panics on EDK2 (aarch64).
# x86_64 ships vmlinuz (compressed); aarch64 ships vmlinux (uncompressed).
# nullglob: unmatched patterns expand to nothing, so the unused arch's
# pattern doesn't survive as a literal string under set -o pipefail.
mkdir -p /home/vagrant/extracted
shopt -s nullglob
kernels=(/mnt/boot/vmlinuz-* /mnt/boot/vmlinux-*)
initrds=(/mnt/boot/initrd.img-*)
shopt -u nullglob
kernel=$(printf '%s\n' "${kernels[@]}" | sort -V | tail -1)
initrd=$(printf '%s\n' "${initrds[@]}" | sort -V | tail -1)
cp -L "$kernel" /home/vagrant/extracted/kernel
cp -L "$initrd" /home/vagrant/extracted/initrd
zbm_args=$(zfs get -H -o value org.zfsbootmenu:commandline rpool/ROOT)
[ "$zbm_args" = "-" ] && zbm_args=""
printf 'root=zfs:rpool/ROOT/%s%s' "$UBUNTU_NAME" "${zbm_args:+ $zbm_args}" >/home/vagrant/extracted/cmdline
chown -R vagrant:vagrant /home/vagrant/extracted

# Only the rpool root dataset itself remains mounted in the host namespace.
zfs unmount "rpool/ROOT/$UBUNTU_NAME"
sync

# Export. The intermittent "pool is busy" failures we used to hit are
# udev/systemd handles lingering on the freshly-bootstrapped root
# (matches upstream openzfs/zfs#16036), not autotrim — `zpool export
# -f` doesn't bypass the spa_refcount EBUSY gate either, so force is
# pointless. udevadm settle drains pending uevents; one retry after 5s
# covers the rare slow-drain case. If both attempts fail the pool is
# genuinely wedged and we want the build to fail rather than ship an
# image that wasn't cleanly quiesced.
udevadm settle
if ! zpool export rpool; then
  sleep 5
  zpool export rpool
fi
