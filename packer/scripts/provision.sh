#!/bin/bash
set -euxo pipefail

SSH_KEY_PUB=$(cat /home/vagrant/.ssh/authorized_keys)

case $SOURCE_NAME in
ubuntu-zfs)
  DISKS=(/dev/vdb)
  LAYOUT=""
  ;;
ubuntu-zfs-lab)
  DISKS=(/dev/vdb /dev/vdc /dev/vdd)
  LAYOUT="mirror"
  ;;
*)
  echo >&2 "Unknown build $SOURCE_NAME"
  exit 1
  ;;
esac

# Ensure APT doesn't asks questions

export DEBIAN_FRONTEND=noninteractive

# Install helpers

apt-get update
apt-get install --yes debootstrap gdisk zfsutils-linux

# Generate /etc/hostid

zgenhostid -f

# Create partitions

for disk in "${DISKS[@]}"; do
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
  rpool $LAYOUT "${DISKS[@]/%/3}"

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

# Update device symlinks

udevadm trigger

# Install Ubuntu

debootstrap "$UBUNTU_NAME" /mnt "$UBUNTU_MIRROR"

# Copy files into the new install

cp /etc/hostid /mnt/etc
cp /etc/resolv.conf /mnt/etc

# Configure networking. Match by driver so the same image works under any
# qemu device topology (packer's vs. testrole's direct-kernel boot give the
# NIC different kernel names; both use virtio_net).
cat <<EOF >/mnt/etc/netplan/01-netcfg.yaml
network:
  version: 2
  ethernets:
    primary:
      match:
        driver: virtio_net
      dhcp4: true
      dhcp-identifier: mac
EOF

# Chroot into the new OS, inside a private mount namespace so every mount
# made here (proc, sys, dev, dev/pts, plus /boot/efi mounted by chroot.sh)
# is torn down by the kernel the instant unshare exits — no race with host
# services (udisks2, multipathd, snapd's LXD glue) reaching into our mounts
# and leaving stale references on /mnt.
#
# Env propagation: chroot inherits the calling shell's env, so packer's
# UBUNTU_*/ZBM_*/REFIND_NAME (already exported via the shell provisioner
# env block) flow straight through. Script-local vars must be exported
# explicitly; DISKS is a bash array so flatten it to a space-separated
# string (chroot.sh re-parses with `read -r -a`). New vars added later
# need only be exported here, not enumerated on the chroot line.
# shellcheck disable=SC2178  # array→string is intentional for export across the chroot bash invocation
export DISKS="${DISKS[*]}"
export LAYOUT SSH_KEY_PUB
unshare --mount --propagation private bash <<'EOF'
set -euxo pipefail
mount -t proc proc /mnt/proc
mount -t sysfs sys /mnt/sys
mount -B /dev /mnt/dev
mount -t devpts pts /mnt/dev/pts
chroot /mnt bash </home/vagrant/chroot.sh
EOF

# Only the rpool root dataset itself remains mounted in the host namespace.
zfs unmount "rpool/ROOT/$UBUNTU_NAME"
sync

# Export with backoff. The pool was created with autotrim=on, and the
# heavy writes from chroot.sh (apt installs + ZBM copy) leave vdev_autotrim
# batching TRIMs in the background; each in-flight TRIM bumps spa_refcount
# enough to make even `zpool export -f` fail with "pool is busy". Retrying
# rides out the transient quiet windows between TRIM batches.
exported=
for delay in 2 5 10 15 30; do
  sleep "$delay"
  if zpool export rpool; then
    exported=1
    break
  fi
  echo "rpool export attempt failed; waited ${delay}s, retrying" >&2
done

if [ -z "$exported" ]; then
  # Don't block the build if even -f fails. systemd's shutdown sequence
  # tears the pool down at reboot time, and ZFS recovers from unclean
  # export on next import (uberblocks committed every TXG, ~5s).
  zpool export -f rpool || echo "WARNING: rpool export failed; deferring to systemd shutdown" >&2
  sync
fi
