#!/bin/bash
set -euxo pipefail

HOSTNAME=$SOURCE_NAME
USERNAME=vagrant
PASSWORD=vagrant
SSH_KEY_PUB=$(cat /home/vagrant/.ssh/authorized_keys)

case $SOURCE_NAME in
ubuntu-pug | ubuntu-box)
  DISKS=(/dev/vdb)
  LAYOUT=""
  ;;
ubuntu-lab)
  DISKS=(/dev/vdb /dev/vdc /dev/vdd)
  LAYOUT="mirror"
  ;;
*)
  echo >&2 "Unknown build $SOURCE_NAME"
  exit 1
  ;;
esac

case $(uname -m) in
aarch64)
  UBUNTU_MIRROR="http://apt.lab.fahm.fr/ports.ubuntu.com/ubuntu-ports/"
  UBUNTU_MIRROR_SECURITY="http://apt.lab.fahm.fr/ports.ubuntu.com/ubuntu-ports/"
  ;;
x86_64)
  UBUNTU_MIRROR="http://apt.lab.fahm.fr/archive.ubuntu.com/ubuntu/"
  UBUNTU_MIRROR_SECURITY="http://apt.lab.fahm.fr/security.ubuntu.com/ubuntu/"
  ;;
*)
  echo >&2 "Unknown machine name $MACHINE"
  exit 1
  ;;
esac

# Ensure APT doesn't asks questions

export DEBIAN_FRONTEND=noninteractive

# Confirm EFI support:

dmesg | grep -i efivars

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

  sync
  sleep 2

  sgdisk -n1:1M:+512M -t1:EF00 "$disk"       # EFI (EF00 = EFI system partition)
  sgdisk -a1 -n4:24K:+1000K -t4:EF02 "$disk" # MBR booting (EF02 = BIOS boot partition)

  if [ "$LAYOUT" = "" ]; then
    sgdisk -n2:0:+500M -t2:8200 "$disk" # Swap (8200 = Linux Swap)
  else
    sgdisk -n2:0:+500M -t2:FD00 "$disk" # Swap (FD00 = Linux RAID)
  fi

  sgdisk -p "$disk"
  if [ "$SOURCE_NAME" = "ubuntu-lab" ]; then
    sgdisk -n5:-2G:0 -t5:BF01 "$disk" # metadata vdev (BF01 = Solaris /usr & Mac ZFS, default when doing zpool create)
  fi
  sgdisk -n3:0:0 -t3:BF00 "$disk" # rpool (BF00 = Solaris root)

  sgdisk -p "$disk"

  sync
  sleep 2
done

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

zfs create -o canmount=off    -o mountpoint=none rpool/ROOT
zfs create -o canmount=noauto -o mountpoint=/    "rpool/ROOT/$UBUNTU_NAME"

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

# Configure networking

apt-get install --yes net-tools

IFACE=$(route | grep '^default' | grep -o '[^ ]*$')

cat <<EOF >/mnt/etc/netplan/01-netcfg.yaml
network:
  version: 2
  ethernets:
    $IFACE:
      dhcp4: true
      dhcp-identifier: mac
EOF

# Chroot into the new OS

mount -t proc proc /mnt/proc
mount -t sysfs sys /mnt/sys
mount -B /dev /mnt/dev
mount -t devpts pts /mnt/dev/pts

chroot /mnt env DISKS="${DISKS[*]}" LAYOUT="$LAYOUT" HOSTNAME="$HOSTNAME" USERNAME="$USERNAME" PASSWORD="$PASSWORD" SSH_KEY_PUB="$SSH_KEY_PUB" UBUNTU_NAME="$UBUNTU_NAME" UBUNTU_MIRROR="$UBUNTU_MIRROR" UBUNTU_MIRROR_SECURITY="$UBUNTU_MIRROR_SECURITY" bash </home/vagrant/chroot.sh

# unmount everything
umount -n -R /mnt

# Export the zpool and reboot
zpool export rpool
reboot
