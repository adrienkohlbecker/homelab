#!/bin/bash
set -euxo pipefail

read -r -a DISKS <<<"$DISKS"

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

apt-get install --yes dosfstools zfs-initramfs zfsutils-linux linux-generic

systemctl enable zfs.target
systemctl enable zfs-import-cache
systemctl enable zfs-mount
systemctl enable zfs-import.target

for disk in "${DISKS[@]}"; do
  mkdosfs -F 32 -s 1 -n EFI "${disk}-part1"
done
mkdir /boot/efi
echo "$(blkid -s UUID | grep "$(readlink -f "${DISKS[0]}-part1")" | cut -d' ' -f2)" \
  /boot/efi vfat defaults 0 0 >>/etc/fstab
mount /boot/efi

if [ "$LAYOUT" = "" ]; then
  mkswap -f "${DISKS[0]}-part2"
  echo "$(blkid -s UUID | grep "$(readlink -f "${DISKS[0]}-part2")" | cut -d' ' -f2)" \
    none swap discard 0 0 >>/etc/fstab
  swapon -a
else
  if [ "$LAYOUT" = "mirror" ]; then
    level="mirror"
  elif [ "$LAYOUT" = "raidz" ]; then
    level="raid5"
  elif [ "$LAYOUT" = "raidz2" ]; then
    level="raid6"
  else
    echo >&2 "Unexpected layout $LAYOUT"
    exit 1
  fi
  apt-get install --yes mdadm
  mdadm --create /dev/md0 --metadata=1.2 --level="$level" --raid-devices="${#DISKS[@]}" "${DISKS[@]/%/-part2}"
  mkswap -f /dev/md0
  echo "/dev/disk/by-uuid/$(blkid -s UUID -o value /dev/md0) none swap discard 0 0" >>/etc/fstab
fi

update-initramfs -c -k all

cp /usr/share/systemd/tmp.mount /etc/systemd/system/
systemctl enable tmp.mount

addgroup --system lpadmin
addgroup --system lxd
addgroup --system sambashare

apt-get install --yes openssh-server

apt-get install --yes curl
mkdir -p /boot/efi/EFI/ZBM
# curl -o /boot/efi/EFI/ZBM/VMLINUZ.EFI -L https://get.zfsbootmenu.org/efi
curl -o /boot/efi/EFI/ZBM/VMLINUZ.EFI -L https://gitea.lab.fahm.fr/api/packages/adrienkohlbecker/generic/zfsbootmenu/3.0.1/zfsbootmenu-recovery-aarch64-v3.0.1-linux6.1.EFI
cp /boot/efi/EFI/ZBM/VMLINUZ.EFI /boot/efi/EFI/ZBM/VMLINUZ-BACKUP.EFI

apt-get install --yes refind
cat /boot/refind_linux.conf
rm -f /boot/refind_linux.conf

cat <<EOF >>/boot/efi/EFI/refind/refind.conf
menuentry "Ubuntu (ZBM)" {
    loader /EFI/ZBM/VMLINUZ.EFI
    options "quit loglevel=0 zbm.skip"
}

menuentry "Ubuntu (ZBM Menu)" {
    loader /EFI/ZBM/VMLINUZ.EFI
    options "quit loglevel=0 zbm.show"
}

default_selection "Ubuntu (ZBM)"
timeout 10
EOF

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

apt-get install --yes open-vm-tools
