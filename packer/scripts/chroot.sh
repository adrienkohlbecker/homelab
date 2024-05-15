#!/bin/bash
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update

locale-gen en_US.UTF-8
update-locale --reset LANG=en_US.UTF-8
ln -fs /usr/share/zoneinfo/Etc/UTC /etc/localtime

cat <<EOF >/etc/default/console-setup
# CONFIGURATION FILE FOR SETUPCON

# Consult the console-setup(5) manual page.

ACTIVE_CONSOLES="/dev/tty[1-6]"

CHARMAP="UTF-8"

CODESET="Latin15"
FONTFACE="Fixed"
FONTSIZE="8x16"

VIDEOMODE=

# The following is an example how to use a braille font
# FONT='lat9w-08.psf.gz brl-8x8.psf'
EOF

cat <<EOF >/etc/default/keyboard
# KEYBOARD CONFIGURATION FILE

# Consult the keyboard(5) manual page.

XKBMODEL="pc105"
XKBLAYOUT="fr"
XKBVARIANT="mac"
XKBOPTIONS=""
EOF

apt-get install --yes dosfstools

mkdosfs -F 32 -s 1 -n EFI ${DISK}-part1
mkdir /boot/efi
echo "$(blkid -s UUID | grep "$(readlink -f ${DISK}-part1)" | cut -d' ' -f2)" \
  /boot/efi vfat defaults 0 0 >>/etc/fstab
mount /boot/efi

mkdir /boot/efi/grub /boot/grub
echo /boot/efi/grub /boot/grub none defaults,bind 0 0 >>/etc/fstab
mount /boot/grub

apt-get install --yes \
  grub-efi-$ARCH grub-efi-$ARCH-signed linux-image-generic \
  shim-signed zfs-initramfs
apt-get purge --yes os-prober

mkswap -f ${DISK}-part2
echo "$(blkid -s UUID | grep "$(readlink -f ${DISK}-part2)" | cut -d' ' -f2)" \
  none swap discard 0 0 >>/etc/fstab
swapon -a

cp /usr/share/systemd/tmp.mount /etc/systemd/system/
systemctl enable tmp.mount

addgroup --system lpadmin
addgroup --system lxd
addgroup --system sambashare

apt-get install --yes openssh-server

grub-probe /boot

update-initramfs -c -k all

sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT=""/GRUB_CMDLINE_LINUX_DEFAULT="init_on_alloc=0"/' /etc/default/grub
sed -i 's/GRUB_TIMEOUT_STYLE/#GRUB_TIMEOUT_STYLE/' /etc/default/grub
sed -i 's/GRUB_TIMEOUT=0/GRUB_TIMEOUT=5\nGRUB_RECORDFAIL_TIMEOUT=5/' /etc/default/grub
sed -i 's/#GRUB_TERMINAL=console/GRUB_TERMINAL=console/' /etc/default/grub

update-grub

grub-install --target=$ARCH_GRUB-efi --efi-directory=/boot/efi \
  --bootloader-id=ubuntu --recheck --no-floppy

mkdir /etc/zfs/zfs-list.cache
touch /etc/zfs/zfs-list.cache/bpool
touch /etc/zfs/zfs-list.cache/rpool
zed -F &
zfs set canmount=on bpool/BOOT/jammy
zfs set canmount=on rpool/ROOT/jammy
sync
sleep 2
jobs -p | xargs kill
sed -Ei "s|/chroot/?|/|" /etc/zfs/zfs-list.cache/*

adduser --disabled-password --gecos "" "$USERNAME"
echo -e "$USERNAME:$PASSWORD" | chpasswd -c SHA256
cp -a /etc/skel/. "/home/$USERNAME"

mkdir "/home/$USERNAME/.ssh"
echo "$SSH_KEY_PUB" >"/home/$USERNAME/.ssh/authorized_keys"
chmod 0700 "/home/$USERNAME/.ssh"
chmod 0600 "/home/$USERNAME/.ssh/authorized_keys"

chown -R "$USERNAME:$USERNAME" "/home/$USERNAME"
usermod -a -G adm,cdrom,dip,lpadmin,lxd,plugdev,sambashare,sudo "$USERNAME"

echo "$USERNAME ALL=(ALL) NOPASSWD: ALL" >/etc/sudoers.d/$USERNAME
chown root:root /etc/sudoers.d/$USERNAME
chmod 400 /etc/sudoers.d/$USERNAME

apt-get install --yes open-vm-tools
