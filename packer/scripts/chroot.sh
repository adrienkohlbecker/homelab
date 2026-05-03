#!/bin/bash
set -euxo pipefail

read -r -a DISKS <<<"$DISKS"

# Local constants. SOURCE_NAME comes from packer's shell-provisioner
# env block; DISKS, LAYOUT, SSH_KEY_PUB are exported by provision.sh.
HOSTNAME="$SOURCE_NAME"
USERNAME=vagrant
PASSWORD=vagrant

export DEBIAN_FRONTEND=noninteractive

# Retry transient apt failures (Nexus restart, packet loss). Persists
# in the shipped image; ansible runs see the same resilience.
# Acquire::Retries::Delay (apt 2.7+ in noble) adds backoff between
# attempts so a Nexus restart of a few seconds isn't burned through
# instantly; apt on jammy retries immediately.
echo 'Acquire::Retries "3";' >/etc/apt/apt.conf.d/80-retries
if [ "$UBUNTU_NAME" != "jammy" ]; then
  echo 'Acquire::Retries::Delay "true";' >>/etc/apt/apt.conf.d/80-retries
fi

# Set a hostname

hostname "$HOSTNAME"
echo "$HOSTNAME" >/etc/hostname

cat <<EOF >/etc/hosts
127.0.0.1       localhost
127.0.1.1       $HOSTNAME
::1             ip6-localhost ip6-loopback
fe00::0         ip6-localnet
ff00::0         ip6-mcastprefix
ff02::1         ip6-allnodes
ff02::2         ip6-allrouters
EOF

# Configure apt

cat <<EOF >/etc/apt/sources.list
# Uncomment the deb-src entries if you need source packages

deb $UBUNTU_MIRROR $UBUNTU_NAME main restricted universe multiverse
# deb-src $UBUNTU_MIRROR $UBUNTU_NAME main restricted universe multiverse

deb $UBUNTU_MIRROR $UBUNTU_NAME-updates main restricted universe multiverse
# deb-src $UBUNTU_MIRROR $UBUNTU_NAME-updates main restricted universe multiverse

deb $UBUNTU_MIRROR $UBUNTU_NAME-backports main restricted universe multiverse
# deb-src $UBUNTU_MIRROR $UBUNTU_NAME-backports main restricted universe multiverse

deb $UBUNTU_MIRROR_SECURITY $UBUNTU_NAME-security main restricted universe multiverse
# deb-src $UBUNTU_MIRROR_SECURITY $UBUNTU_NAME-security main restricted universe multiverse
EOF

# Configure locale

locale-gen en_US.UTF-8
update-locale --reset LANG=en_US.UTF-8

# Configure timezone

ln -fs /usr/share/zoneinfo/Etc/UTC /etc/localtime

# Configure console

cat <<EOF >/etc/default/console-setup
# CONFIGURATION FILE FOR SETUPCON

# Consult the console-setup(5) manual page.

ACTIVE_CONSOLES="/dev/tty[1-6]"

CHARMAP="UTF-8"

CODESET="Lat15"
FONTFACE=""
FONTSIZE=""

VIDEOMODE=

# The following is an example how to use a braille font
# FONT='lat9w-08.psf.gz brl-8x8.psf'
EOF

# Configure keyboard

cat <<EOF >/etc/default/keyboard
# KEYBOARD CONFIGURATION FILE

# Consult the keyboard(5) manual page.

XKBMODEL="pc105"
XKBLAYOUT="fr"
XKBVARIANT="mac"
XKBOPTIONS=""

BACKSPACE="guess"
EOF

# Update the repository cache

apt-get update

# Update system

apt-get upgrade --yes

# Install additional base packages
# linux-generic Recommends `grub-pc | grub-efi-amd64 | grub-efi-ia32 |
# grub | lilo` (transitively via linux-image-X.X.X-generic). We boot
# via ZFSBootMenu + rEFInd, so block the alternation by holding all
# grub variants and lilo. Held packages are silently skipped from
# Recommends; the glob covers future grub sub-packages without an
# enumerated list. Other useful recommends (thermald, etc.) come in
# normally.

apt-mark hold 'grub*' lilo
apt-get install --yes linux-generic

# Install required packages

apt-get install --yes dosfstools zfs-initramfs zfsutils-linux

# Enable systemd ZFS services

systemctl enable zfs.target
systemctl enable zfs-import-cache
systemctl enable zfs-mount
systemctl enable zfs-import.target

# Rebuild the initramfs

update-initramfs -c -k all

# Set ZFSBootMenu properties on datasets

zfs set org.zfsbootmenu:commandline="" "rpool/ROOT"

# Create efi & swap

if [ "$LAYOUT" = "" ]; then
  EFI_DEVICE="${DISKS[0]}1"
  SWAP_DEVICE="${DISKS[0]}2"
else
  apt-get install --yes mdadm

  # This configuration exploits the fact that, with version 1.0, mdraid metadata will be written to the end of each partition.
  # Newer metadata versions would be written to the beginning of each partition, and the system firmware would fail to
  # recognize each component as a valid EFI system partition.
  mdadm --create /dev/md/efi --name=efi --metadata=1.0 --level="raid1" --raid-devices="${#DISKS[@]}" "${DISKS[@]/%/1}"
  mdadm --detail --brief /dev/md/efi >>/etc/mdadm/mdadm.conf
  EFI_DEVICE=/dev/md/efi

  mdadm --create /dev/md/swap --name=swap --metadata=1.2 --level="raid0" --raid-devices="${#DISKS[@]}" "${DISKS[@]/%/2}"
  mdadm --detail --brief /dev/md/swap >>/etc/mdadm/mdadm.conf
  SWAP_DEVICE=/dev/md/swap
fi

# Create filesystems

mkdosfs -F 32 -s 1 -n EFI "$EFI_DEVICE"
mkswap -f "$SWAP_DEVICE"

sync
sleep 2

# Get UUIDs, they exist only after the filesystem has been created

blkid

if [ "$LAYOUT" = "" ]; then
  EFI_DEVICE="/dev/disk/by-uuid/$(blkid -s UUID -o value "$EFI_DEVICE")"
  SWAP_DEVICE="/dev/disk/by-uuid/$(blkid -s UUID -o value "$SWAP_DEVICE")"
fi

# Update fstab

echo "$EFI_DEVICE /boot/efi vfat defaults 0 0" >>/etc/fstab
echo "$SWAP_DEVICE none swap discard 0 0" >>/etc/fstab

# Update device symlinks

udevadm trigger

# Update initramfs

update-initramfs -u -k all

# Mount EFI filesystem

mkdir -p /boot/efi
mount /boot/efi

# Install ZFSBootMenu
#
# ZBM is built + uploaded out-of-band by `mise run zbm:build && zbm:upload`.
# The Gitea package holds one stable filename per (version, arch);
# zbm:upload deletes any existing copy before PUT, so a rebuild
# propagates here without source edits. Bump mise.toml's zbm_version
# when moving to a new upstream release; the value is plumbed through
# packer (var.zbm_version) and provision.sh into $ZBM_VERSION here, so
# this script holds no version literal of its own.
#
# Components mode: the artifact is a tarball with kernel + initrd, not a
# unified UKI .EFI. rEFInd does the kernel handoff via loader/initrd
# directives — the systemd-boot aarch64 EFI stub silently fails under
# EDK2 on QEMU virt, and rEFInd's own loader works on both arches.
# rEFInd ships as refind_x64.efi on x86_64 and refind_aa64.efi on aarch64
# ($REFIND_NAME, also resolved upstream in HCL).
ZBM_URL="https://gitea.lab.fahm.fr/api/packages/adrienkohlbecker/generic/zfsbootmenu/${ZBM_VERSION}/zfsbootmenu-v${ZBM_VERSION}-${ZBM_ARCH}.tar.gz"

apt-get install --yes curl
mkdir -p /boot/efi/EFI/ZBM
curl -fL --retry 3 --retry-connrefused -o /tmp/zbm.tar.gz "$ZBM_URL"
EXPECTED_SUM="$(curl -fsSL --retry 3 --retry-connrefused "$ZBM_URL.sha256sum" | awk '{print $1}')"
echo "$EXPECTED_SUM  /tmp/zbm.tar.gz" | sha256sum -c -
tar -xzf /tmp/zbm.tar.gz -C /boot/efi/EFI/ZBM/ --no-same-owner
rm /tmp/zbm.tar.gz

# x86_64 emits vmlinuz-bootmenu (compressed); aarch64 emits vmlinux-bootmenu
# (uncompressed). Capture the actual filename for the rEFInd menuentry.
ZBM_KERNEL="$(basename /boot/efi/EFI/ZBM/vmlin*-bootmenu)"

# Configure rEFInd

mountpoint -q /sys/firmware/efi/efivars || mount -t efivarfs efivarfs /sys/firmware/efi/efivars
apt-get install --yes refind
refind-install
rm /boot/refind_linux.conf

cat <<EOF >/boot/efi/EFI/refind/refind.conf
timeout 1
default_selection "Ubuntu (ZBM)"
dont_scan_dirs /EFI/ZBM

menuentry "Ubuntu (ZBM)" {
    loader /EFI/ZBM/${ZBM_KERNEL}
    initrd /EFI/ZBM/initramfs-bootmenu.img
    options "quiet loglevel=0 zbm.skip"
}

menuentry "Ubuntu (ZBM Menu)" {
    loader /EFI/ZBM/${ZBM_KERNEL}
    initrd /EFI/ZBM/initramfs-bootmenu.img
    options "quiet loglevel=0 zbm.show"
}
EOF

# Configure EFI boot entry — only rEFInd, since ZBM is no longer a
# bootable EFI binary (Components mode = kernel + initrd as separate
# files, loaded by rEFInd).

apt-get install --yes efibootmgr

# On the multi-disk mdadm-EFI mirror, register one boot entry per disk
# so the system survives losing any single disk — firmware only follows
# paths it knows about, and an entry is per-disk regardless of whether
# the ESP content is mirrored. Single-disk variants get a single bare
# "rEFInd" entry (unchanged).
if [ "${#DISKS[@]}" -eq 1 ]; then
  efibootmgr -c -d "${DISKS[0]}" -p 1 \
    -L "rEFInd" \
    -l "\\EFI\\refind\\${REFIND_NAME}"
else
  for idx in "${!DISKS[@]}"; do
    efibootmgr -c -d "${DISKS[$idx]}" -p 1 \
      -L "rEFInd (disk ${idx})" \
      -l "\\EFI\\refind\\${REFIND_NAME}"
  done
fi

# Enable tmp mount

cp /usr/share/systemd/tmp.mount /etc/systemd/system/
systemctl enable tmp.mount

# Add more packages

apt-get install --yes openssh-server qemu-guest-agent

# Configure vagrant user

adduser --disabled-password --gecos "" "$USERNAME"
echo -e "$USERNAME:$PASSWORD" | chpasswd -c SHA256
cp -a /etc/skel/. "/home/$USERNAME"

mkdir "/home/$USERNAME/.ssh"
echo "$SSH_KEY_PUB" >"/home/$USERNAME/.ssh/authorized_keys"
chmod 0700 "/home/$USERNAME/.ssh"
chmod 0600 "/home/$USERNAME/.ssh/authorized_keys"

chown -R "$USERNAME:$USERNAME" "/home/$USERNAME"
usermod -a -G adm,sudo "$USERNAME"

echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" >"/etc/sudoers.d/$USERNAME"
chown root:root "/etc/sudoers.d/$USERNAME"
chmod 400 "/etc/sudoers.d/$USERNAME"

# Reset apt sources to upstream so the shipped image isn't pinned to a
# Nexus-internal URL. Build-time installs above used $UBUNTU_MIRROR
# (Nexus by default); ansible's mirror_apt_ubuntu_* may rewrite this
# again on first run, but the at-rest image must point at canonical
# Ubuntu mirrors.
cat <<EOF >/etc/apt/sources.list
# Uncomment the deb-src entries if you need source packages

deb $UBUNTU_MIRROR_UPSTREAM $UBUNTU_NAME main restricted universe multiverse
# deb-src $UBUNTU_MIRROR_UPSTREAM $UBUNTU_NAME main restricted universe multiverse

deb $UBUNTU_MIRROR_UPSTREAM $UBUNTU_NAME-updates main restricted universe multiverse
# deb-src $UBUNTU_MIRROR_UPSTREAM $UBUNTU_NAME-updates main restricted universe multiverse

deb $UBUNTU_MIRROR_UPSTREAM $UBUNTU_NAME-backports main restricted universe multiverse
# deb-src $UBUNTU_MIRROR_UPSTREAM $UBUNTU_NAME-backports main restricted universe multiverse

deb $UBUNTU_MIRROR_SECURITY_UPSTREAM $UBUNTU_NAME-security main restricted universe multiverse
# deb-src $UBUNTU_MIRROR_SECURITY_UPSTREAM $UBUNTU_NAME-security main restricted universe multiverse
EOF
