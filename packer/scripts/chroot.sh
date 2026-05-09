#!/bin/bash
set -euxo pipefail

# Env consumed by this script:
# - From packer's shell-provisioner env block (qemu.pkr.hcl):
#   UBUNTU_NAME, UBUNTU_MIRROR, UBUNTU_MIRROR_SECURITY,
#   UBUNTU_MIRROR_UPSTREAM, UBUNTU_MIRROR_SECURITY_UPSTREAM,
#   SSH_KEY_PUB.
# - Inherited from provision.sh: DISKS, DISKS_COUNT, LAYOUT,
#   PARTITIONS_EFI, PARTITIONS_SWAP, HOSTNAME, USERNAME.
# All list-shaped vars are space-delimited strings (bash arrays don't
# survive `export`); use them unquoted to word-split.

# ZFSBootMenu version installed into the shipped image. Independent
# from mise.toml's [vars] zbm_version, which controls what `mise run
# zbm:build` produces — bump that to build a new tarball, bump this
# to start shipping it. They line up once a new tarball has been
# uploaded to Gitea (`mise run zbm:upload`) and verified.
ZBM_VERSION="3.1.0"

# Arch-derived constants: rEFInd EFI binary names + ZBM tarball arch
# token. The build VM and the shipped image are always the same arch,
# so detecting via `uname -m` here is equivalent to passing in from
# packer. Fail loud on unsupported arches; adding aarch64 vs x86_64
# also requires updating qemu.pkr.hcl's arch_table.
ZBM_ARCH=$(uname -m)
case $ZBM_ARCH in
x86_64)
  REFIND_NAME=refind_x64.efi
  REFIND_FALLBACK_NAME=BOOTX64.EFI
  ;;
aarch64)
  REFIND_NAME=refind_aa64.efi
  REFIND_FALLBACK_NAME=BOOTAA64.EFI
  ;;
*)
  echo >&2 "Unsupported arch: $ZBM_ARCH"
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

# Configure apt. Called twice: once now with the build-time mirror
# ($UBUNTU_MIRROR, defaults to Nexus), and once at the very end with
# the upstream pair so the shipped image points at canonical Ubuntu
# URLs regardless of build-time routing.
write_sources_list() {
  cat <<EOF >/etc/apt/sources.list
# Uncomment the deb-src entries if you need source packages

deb $1 $UBUNTU_NAME main restricted universe multiverse
# deb-src $1 $UBUNTU_NAME main restricted universe multiverse

deb $1 $UBUNTU_NAME-updates main restricted universe multiverse
# deb-src $1 $UBUNTU_NAME-updates main restricted universe multiverse

deb $1 $UBUNTU_NAME-backports main restricted universe multiverse
# deb-src $1 $UBUNTU_NAME-backports main restricted universe multiverse

deb $2 $UBUNTU_NAME-security main restricted universe multiverse
# deb-src $2 $UBUNTU_NAME-security main restricted universe multiverse
EOF
}

write_sources_list "$UBUNTU_MIRROR" "$UBUNTU_MIRROR_SECURITY"

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
# grub variants. Held packages are silently skipped from Recommends;
# the glob covers future grub sub-packages without an enumerated list.
# lilo is not in the archive (no candidate), so apt won't pick it.
# Other useful recommends (thermald, etc.) come in normally.

apt-mark hold 'grub*'
apt-get install --yes linux-generic

# Install required packages

apt-get install --yes dosfstools zfs-initramfs zfsutils-linux

# Enable systemd ZFS services

systemctl enable zfs.target
systemctl enable zfs-import-cache
systemctl enable zfs-mount
systemctl enable zfs-import.target

# Set ZFSBootMenu properties on datasets

zfs set org.zfsbootmenu:commandline="" "rpool/ROOT"

# Create efi & swap

if [ "$LAYOUT" = "" ]; then
  EFI_DEVICE="$PARTITIONS_EFI"
  SWAP_DEVICE="$PARTITIONS_SWAP"
else
  apt-get install --yes mdadm

  # arch-chroot mounts /sys read-only inside the chroot. mdadm 4.5 (resolute)
  # writes N to /sys/module/md_mod/parameters/legacy_async_del_gendisk at
  # startup to opt out of the deprecated async del_gendisk path; on a ro /sys
  # the open fails with "init md module parameters fail" and mdadm aborts.
  # Debian #1125390 / md-raid-utilities/mdadm#228 — upstream fix in 4.5-3
  # reorders the modprobe before the write but doesn't help us since the
  # mount is still ro. Remount rw for the duration of the chroot.
  mount -o remount,rw /sys

  # This configuration exploits the fact that, with version 1.0, mdraid metadata will be written to the end of each partition.
  # Newer metadata versions would be written to the beginning of each partition, and the system firmware would fail to
  # recognize each component as a valid EFI system partition.
  # Some OEM firmwares (Asus consumer, certain Supermicro X11/X12) scan
  # ESPs more aggressively and may refuse a member partition; the
  # per-disk efibootmgr entries below are the survival mechanism if
  # one disk's path stops working. Validate on your firmware before
  # committing to this layout for new bare-metal hosts.
  # --bitmap=none: mdadm 4.4+ prompts for write-intent bitmap on raid1
  # creation; bitmap data is incompatible with metadata=1.0 ESPs (the
  # firmware would refuse the partition), so suppress the prompt with an
  # explicit no.
  # shellcheck disable=SC2086  # word-splitting on PARTITIONS_EFI is the point
  mdadm --create /dev/md/efi --name=efi --metadata=1.0 --level="raid1" --bitmap=none --raid-devices="$DISKS_COUNT" $PARTITIONS_EFI
  mdadm --detail --brief /dev/md/efi >>/etc/mdadm/mdadm.conf
  EFI_DEVICE=/dev/md/efi

  # shellcheck disable=SC2086  # word-splitting on PARTITIONS_SWAP is the point
  mdadm --create /dev/md/swap --name=swap --metadata=1.2 --level="raid0" --raid-devices="$DISKS_COUNT" $PARTITIONS_SWAP
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

# Pull all available modules into initramfs (rather than just the
# build host's currently-loaded set) so the shipped image boots on
# bare-metal hardware whose controllers/NICs the build host didn't
# happen to have. Costs ~30 MiB qcow2-compressed. Set right before
# the final rebuild so we don't have to worry about earlier postinst-
# driven update-initramfs calls.
sed -i 's/^MODULES=.*/MODULES=most/' /etc/initramfs-tools/initramfs.conf

# Update initramfs

update-initramfs -u -k all

# Mount EFI filesystem

mkdir -p /boot/efi
mount /boot/efi

# Install ZFSBootMenu
#
# ZBM is built + uploaded out-of-band by `mise run zbm:build && zbm:upload`
# (mise.toml's [vars] zbm_version drives those). The version installed
# here is decoupled — it's the $ZBM_VERSION constant set at the top of
# this script. Bump it once a new tarball has been built + uploaded to
# Gitea and verified.
#
# Components mode: the artifact is a tarball with kernel + initrd, not a
# unified UKI .EFI. rEFInd does the kernel handoff via loader/initrd
# directives — the systemd-boot aarch64 EFI stub silently fails under
# EDK2 on QEMU virt, and rEFInd's own loader works on both arches.
# rEFInd ships as refind_x64.efi on x86_64 and refind_aa64.efi on aarch64
# ($REFIND_NAME, derived from `uname -m` at the top of this script).
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

apt-get install --yes refind
refind-install
rm /boot/refind_linux.conf

# Drop a copy at the firmware fallback path (\EFI\BOOT\BOOT<arch>.EFI)
# so a host whose NVRAM has been wiped (CMOS clear, BIOS update,
# "Restore Defaults") still boots from the ESP. refind-install does
# not write here by default on Debian/Ubuntu.
mkdir -p /boot/efi/EFI/BOOT
cp "/boot/efi/EFI/refind/$REFIND_NAME" "/boot/efi/EFI/BOOT/$REFIND_FALLBACK_NAME"

# aarch64-only: stage the on-pool EFI-stub kernel + initrd onto the ESP
# so rEFInd can launch them directly (menuentry appended below). The
# rEFInd -> ZBM -> kexec chain panics on EDK2/aarch64
# (notes/zbm-aarch64-kexec-bug-report.md), and resolute's vmlinuz
# dropped the dual-format ARM64-Image+PE header that made qemu's
# `-kernel` direct boot work, so the test harness now lets
# EDK2 -> rEFInd -> kernel-EFI-stub take over. That's the same UEFI
# LoadImage path the bug report confirms works (shim -> GRUB ->
# kernel-EFI-stub on the same firmware), just routed through
# refind.conf instead of an NVRAM Boot#### entry.
#
# Caveat: the kernel/initrd staged here are a snapshot of whatever apt
# installed -- a later `apt upgrade linux-image-*` won't refresh them.
# Acceptable for the build-once test images; long-lived hosts would
# need a kernel-install hook to re-stage.
if [ "$ZBM_ARCH" = "aarch64" ]; then
  shopt -s nullglob
  vmlinuz_files=(/boot/vmlinuz-*)
  initrd_files=(/boot/initrd.img-*)
  shopt -u nullglob
  vmlinuz=$(printf '%s\n' "${vmlinuz_files[@]}" | sort -V | tail -1)
  initrd=$(printf '%s\n' "${initrd_files[@]}" | sort -V | tail -1)

  mkdir -p /boot/efi/EFI/Linux
  # jammy/noble ship vmlinuz-* as a gzipped dual-format ARM64-Image+PE
  # binary; EDK2's LoadImage doesn't decompress gzip, so on those
  # releases we have to materialise the underlying Image. resolute
  # ships an uncompressed PE32+ EFI stub directly. `gunzip -t` is a
  # cheap format probe -- 0 means it's a valid gzip stream.
  if gunzip -t "$vmlinuz" 2>/dev/null; then
    gunzip -c "$vmlinuz" >/boot/efi/EFI/Linux/vmlinuz.efi
  else
    cp -L "$vmlinuz" /boot/efi/EFI/Linux/vmlinuz.efi
  fi
  cp -L "$initrd" /boot/efi/EFI/Linux/initrd
fi

# refind.conf. On aarch64 the default points at the staged EFI-stub
# kernel (`Ubuntu (direct)` appended below); the ZBM entries stay
# registered for diagnostics / recovery shell, even though their kexec
# step panics. /EFI/Linux is added to dont_scan_dirs so rEFInd's
# default scan_all_linux_kernels=true doesn't auto-detect vmlinuz.efi
# as a duplicate alongside the explicit menuentry.
if [ "$ZBM_ARCH" = "aarch64" ]; then
  refind_default="Ubuntu (direct)"
  refind_dont_scan="/EFI/ZBM,/EFI/Linux"
else
  refind_default="Ubuntu (ZBM)"
  refind_dont_scan="/EFI/ZBM"
fi

cat <<EOF >/boot/efi/EFI/refind/refind.conf
timeout 1
default_selection "${refind_default}"
dont_scan_dirs ${refind_dont_scan}

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

# console=ttyAMA0 + earlycon target aarch64 virt's pl011 at the
# standard MMIO base for serial-log capture in the test harness;
# bare-metal hosts that lack a pl011 at this address simply ignore the
# directive. rEFInd loads the initrd via the kernel's EFI initrd
# protocol when the `initrd` keyword is set, so it isn't also passed
# on the cmdline.
if [ "$ZBM_ARCH" = "aarch64" ]; then
  cat <<EOF >>/boot/efi/EFI/refind/refind.conf

menuentry "Ubuntu (direct)" {
    loader /EFI/Linux/vmlinuz.efi
    initrd /EFI/Linux/initrd
    options "root=zfs:rpool/ROOT/${UBUNTU_NAME} console=ttyAMA0,115200 earlycon=pl011,0x9000000,115200"
}
EOF
fi

# Configure EFI boot entry — only rEFInd, since ZBM is no longer a
# bootable EFI binary (Components mode = kernel + initrd as separate
# files, loaded by rEFInd) and the aarch64 direct-boot path is now a
# rEFInd menuentry rather than an NVRAM entry.

apt-get install --yes efibootmgr

# On the multi-disk mdadm-EFI mirror, register one boot entry per disk
# so the system survives losing any single disk — firmware only follows
# paths it knows about, and an entry is per-disk regardless of whether
# the ESP content is mirrored. Single-disk variants get a single bare
# "rEFInd" entry (unchanged).
if [ "$DISKS_COUNT" -eq 1 ]; then
  efibootmgr -c -d "$DISKS" -p 1 \
    -L "rEFInd" \
    -l "\\EFI\\refind\\${REFIND_NAME}"
else
  idx=0
  # shellcheck disable=SC2086  # word-splitting on DISKS is the point
  for disk in $DISKS; do
    efibootmgr -c -d "$disk" -p 1 \
      -L "rEFInd (disk ${idx})" \
      -l "\\EFI\\refind\\${REFIND_NAME}"
    idx=$((idx + 1))
  done
fi

# Enable tmp mount. jammy/noble ship tmp.mount as a template under
# /usr/share/systemd/ and leave it disabled — copy + enable. resolute's
# systemd ships /usr/lib/systemd/system/tmp.mount and pre-symlinks it into
# local-fs.target.wants/, so it's enabled out of the box; skip the copy.

if [ -f /usr/share/systemd/tmp.mount ]; then
  cp /usr/share/systemd/tmp.mount /etc/systemd/system/
  systemctl enable tmp.mount
fi

# Add more packages

apt-get install --yes openssh-server qemu-guest-agent

# Disable ssh password authentication. The vagrant account is
# key-only (no password set, see below); other users on the image
# don't exist. Drop a snippet under sshd_config.d so the override
# wins even if /etc/ssh/sshd_config is later edited.
echo 'PasswordAuthentication no' >/etc/ssh/sshd_config.d/00-no-password-auth.conf

# Configure vagrant user

adduser --disabled-password --gecos "" "$USERNAME"
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
write_sources_list "$UBUNTU_MIRROR_UPSTREAM" "$UBUNTU_MIRROR_SECURITY_UPSTREAM"
