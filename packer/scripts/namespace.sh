#!/bin/bash
set -euxo pipefail

read -r -a DISKS <<<"$DISKS"

export DEBIAN_FRONTEND=noninteractive

mkdir /chroot/run
mount -t tmpfs tmpfs /chroot/run
mkdir /chroot/run/lock

debootstrap noble /chroot

hostname "$HOSTNAME"
hostname >/chroot/etc/hostname

cp /etc/hostid /chroot/etc
cp /etc/resolv.conf /chroot/etc

cat <<EOF >/chroot/etc/hosts
127.0.0.1       localhost
127.0.1.1       $HOSTNAME
::1             ip6-localhost ip6-loopback
fe00::0         ip6-localnet
ff00::0         ip6-mcastprefix
ff02::1         ip6-allnodes
ff02::2         ip6-allrouters
EOF

IFACE=$(route | grep '^default' | grep -o '[^ ]*$')

cat <<EOF >/chroot/etc/netplan/01-netcfg.yaml
network:
  version: 2
  ethernets:
    $IFACE:
      dhcp4: true
      dhcp-identifier: mac
EOF

cp /etc/apt/sources.list /chroot/etc/apt/
cp -R /etc/apt/sources.list.d /chroot/etc/apt/

mount --make-private --rbind /dev /chroot/dev
mount --make-private --rbind /proc /chroot/proc
mount --make-private --rbind /sys /chroot/sys

chroot /chroot env DISKS="${DISKS[*]}" HOSTNAME="$HOSTNAME" PASSWORD="$PASSWORD" ARCH="$ARCH" ARCH_GRUB="$ARCH_GRUB" USERNAME="$USERNAME" SSH_KEY_PUB="$SSH_KEY_PUB" LAYOUT="$LAYOUT" bash </home/vagrant/chroot.sh
