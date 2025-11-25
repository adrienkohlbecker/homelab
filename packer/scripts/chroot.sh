#!/bin/bash
set -euxo pipefail

read -r -a DISKS <<<"$DISKS"

export DEBIAN_FRONTEND=noninteractive

case $(uname -m) in
aarch64)
  ZBM_URL="https://gitea.lab.fahm.fr/api/packages/adrienkohlbecker/generic/zfsbootmenu/3.0.1/zfsbootmenu-recovery-aarch64-v3.0.1-linux6.1.EFI"
  ZBM_SUM="8cbe5105ff0d005ff67a4ddcf0d91abed614b07fa281b682e5be5b2bf4929322"
  ;;
x86_64)
  ZBM_URL="https://github.com/zbm-dev/zfsbootmenu/releases/download/v3.0.1/zfsbootmenu-recovery-x86_64-v3.0.1-linux6.12.EFI"
  ZBM_SUM="375ef1a0505bbbd648572c16d83884d5147fa2435508b4717e2749aead676143"
  ;;
*)
  echo >&2 "Unknown machine name $MACHINE"
  exit 1
  ;;
esac

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
# The --no-install-recommends flag is used here to avoid installing recommended, but not strictly needed, packages (including grub2).

apt-get install --yes --no-install-recommends linux-generic

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
  mdadm --create /dev/md/efi --name=any:efi --metadata=1.0 --level="raid1" --raid-devices="${#DISKS[@]}" "${DISKS[@]/%/1}"
  mdadm --detail --brief /dev/md/efi >> /etc/mdadm/mdadm.conf
  EFI_DEVICE=/dev/md/efi

  mdadm --create /dev/md/swap --name=any:swap --metadata=1.2 --level="raid0" --raid-devices="${#DISKS[@]}" "${DISKS[@]/%/2}"
  mdadm --detail --brief /dev/md/swap >> /etc/mdadm/mdadm.conf
  SWAP_DEVICE=/dev/md/swap
fi

# Create filesystems

mkdosfs -F 32 -s 1 -n EFI $EFI_DEVICE
mkswap -f $SWAP_DEVICE

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

apt-get install --yes curl
mkdir -p /boot/efi/EFI/ZBM
curl -o /boot/efi/EFI/ZBM/VMLINUZ.EFI -L $ZBM_URL
echo "$ZBM_SUM  /boot/efi/EFI/ZBM/VMLINUZ.EFI" | sha256sum -c -
cp /boot/efi/EFI/ZBM/VMLINUZ.EFI /boot/efi/EFI/ZBM/VMLINUZ-BACKUP.EFI

# Configure rEFInd

mountpoint -q /sys/firmware/efi/efivars || mount -t efivarfs efivarfs /sys/firmware/efi/efivars
apt-get install --yes refind
refind-install
rm /boot/refind_linux.conf

cat <<EOF >>/boot/efi/EFI/refind/refind.conf
timeout 1
default_selection "Ubuntu (ZBM)"

menuentry "Ubuntu (ZBM)" {
    loader /EFI/ZBM/VMLINUZ.EFI
    options "quit loglevel=0 zbm.skip"
}

menuentry "Ubuntu (ZBM Menu)" {
    loader /EFI/ZBM/VMLINUZ.EFI
    options "quit loglevel=0 zbm.show"
}
EOF

# # Configure EFI boot entries

apt-get install --yes efibootmgr

efibootmgr -c -d "${DISKS[0]}" -p 1 \
  -L "ZFSBootMenu (Backup)" \
  -l '\EFI\ZBM\VMLINUZ-BACKUP.EFI'

efibootmgr -c -d "${DISKS[0]}" -p 1 \
  -L "ZFSBootMenu" \
  -l '\EFI\ZBM\VMLINUZ.EFI'

efibootmgr -c -d "${DISKS[0]}" -p 1 \
  -L "rEFInd" \
  -l '\EFI\refind\refind_x64.efi'

# Enable tmp mount

cp /usr/share/systemd/tmp.mount /etc/systemd/system/
systemctl enable tmp.mount

# Add more packages

apt-get install --yes openssh-server open-vm-tools qemu-guest-agent

# Add missing groups

addgroup --system lpadmin
addgroup --system lxd
addgroup --system sambashare

# Configure vagrant user

adduser --disabled-password --gecos "" "$USERNAME"
echo -e "$USERNAME:$PASSWORD" | chpasswd -c SHA256
cp -a /etc/skel/. "/home/$USERNAME"

mkdir "/home/$USERNAME/.ssh"
echo "$SSH_KEY_PUB" >"/home/$USERNAME/.ssh/authorized_keys"
chmod 0700 "/home/$USERNAME/.ssh"
chmod 0600 "/home/$USERNAME/.ssh/authorized_keys"

chown -R "$USERNAME:$USERNAME" "/home/$USERNAME"
usermod -a -G adm,cdrom,dip,lpadmin,lxd,plugdev,sambashare,sudo "$USERNAME"

echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" >"/etc/sudoers.d/$USERNAME"
chown root:root "/etc/sudoers.d/$USERNAME"
chmod 400 "/etc/sudoers.d/$USERNAME"
