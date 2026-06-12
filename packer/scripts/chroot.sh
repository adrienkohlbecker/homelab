#!/bin/bash
set -euxo pipefail

# Env consumed by this script:
# - From packer's shell-provisioner env block (qemu.pkr.hcl):
#   UBUNTU_NAME, UBUNTU_MIRROR, UBUNTU_MIRROR_SECURITY,
#   UBUNTU_MIRROR_UPSTREAM, UBUNTU_MIRROR_SECURITY_UPSTREAM,
#   SSH_KEY_PUB.
# - Inherited from provision.sh: DISKS, DISKS_COUNT, LAYOUT,
#   PARTITIONS_EFI, PARTITIONS_SWAP, HOSTNAME, USERNAME.
#   PARTITIONS_SWAP is only set for single-disk (LAYOUT=""); mirror
#   variants swap on the rpool/swap zvol provision.sh creates.
# All list-shaped vars are space-delimited strings (bash arrays don't
# survive `export`); use them unquoted to word-split.

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
  ZBM_VERSION=v3.1.0-linux6.18-ci.27033469422.1-x86_64
  CONSOLE_CMDLINE="console=tty0 earlycon=uart8250,io,0x3f8 console=ttyS0,115200"
  ;;
aarch64)
  REFIND_NAME=refind_aa64.efi
  REFIND_FALLBACK_NAME=BOOTAA64.EFI
  ZBM_VERSION=v3.1.0-linux6.18-local.20260605230757-aarch64
  CONSOLE_CMDLINE="console=tty0 earlycon=pl011,0x9000000,115200 console=ttyAMA0,115200"
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

# Retry transient apt failures (Nexus restart, packet loss) on the
# build VM.
# Acquire::Retries::Delay (apt 2.7+ in noble) adds backoff between
# attempts so a Nexus restart of a few seconds isn't burned through
# instantly; apt on jammy retries immediately.
echo 'Acquire::Retries "3";' >/etc/apt/apt.conf.d/80-retries
if [ "$UBUNTU_NAME" != "jammy" ]; then
  echo 'Acquire::Retries::Delay "true";' >>/etc/apt/apt.conf.d/80-retries
fi

# apt-get update exits 0 even when one component's Packages index fails to
# download (Nexus restart, dropped packet), leaving a partial cache that makes
# a later install fail with a baffling "Unable to locate package". Error-Mode
# =any turns a failed fetch into a non-zero exit; the loop retries with backoff
# so a brief blip is absorbed (jammy apt can't do Acquire::Retries::Delay set
# above, so the wait lives here). Same helper as provision.sh's build-VM apt.
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

# Configure apt. Called twice: once now with the build-time mirror
# ($UBUNTU_MIRROR, defaults to Nexus), and once at the very end with
# the upstream pair so the shipped image points at canonical Ubuntu
# URLs regardless of build-time routing.
write_sources_list() {
  # Shape matches roles/apt/templates/sources.list.j2 -- the role
  # overwrites this file on first apply, so keeping the two byte-similar
  # makes a `diff` between packer-baked image and post-apply state a
  # signal that something else moved (mirror substitution, etc).
  cat <<EOF >/etc/apt/sources.list
# See http://help.ubuntu.com/community/UpgradeNotes for how to upgrade to
# newer versions of the distribution.
deb $1 $UBUNTU_NAME main restricted
# deb-src $1 $UBUNTU_NAME main restricted

# # Major bug fix updates produced after the final release of the
# # distribution.
deb $1 $UBUNTU_NAME-updates main restricted
# deb-src $1 $UBUNTU_NAME-updates main restricted

# # N.B. software from this repository is ENTIRELY UNSUPPORTED by the Ubuntu
# # team. Also, please note that software in universe WILL NOT receive any
# # review or updates from the Ubuntu security team.
deb $1 $UBUNTU_NAME universe
# deb-src $1 $UBUNTU_NAME universe
deb $1 $UBUNTU_NAME-updates universe
# deb-src $1 $UBUNTU_NAME-updates universe

# # N.B. software from this repository is ENTIRELY UNSUPPORTED by the Ubuntu
# # team, and may not be under a free licence. Please satisfy yourself as to
# # your rights to use the software. Also, please note that software in
# # multiverse WILL NOT receive any review or updates from the Ubuntu
# # security team.
deb $1 $UBUNTU_NAME multiverse
# deb-src $1 $UBUNTU_NAME multiverse
deb $1 $UBUNTU_NAME-updates multiverse
# deb-src $1 $UBUNTU_NAME-updates multiverse

# # N.B. software from this repository may not have been tested as
# # extensively as that contained in the main release, although it includes
# # newer versions of some applications which may provide useful features.
# # Also, please note that software in backports WILL NOT receive any review
# # or updates from the Ubuntu security team.
deb $1 $UBUNTU_NAME-backports main restricted universe multiverse
# deb-src $1 $UBUNTU_NAME-backports main restricted universe multiverse

deb $2 $UBUNTU_NAME-security main restricted
# deb-src $2 $UBUNTU_NAME-security main restricted
deb $2 $UBUNTU_NAME-security universe
# deb-src $2 $UBUNTU_NAME-security universe
deb $2 $UBUNTU_NAME-security multiverse
# deb-src $2 $UBUNTU_NAME-security multiverse
EOF

  # apt keys /var/lib/apt/lists/ by mirror URL, so changing the mirror
  # here orphans the cached indices. The frozen base suite's InRelease is
  # byte-identical whichever mirror serves it (Nexus just proxies
  # upstream), so the next apt-get update records a content "Hit", skips
  # the re-download, then can't open the list file that was never written
  # under the new URL ("can not open …InRelease"). Drop the cache so each
  # rewrite re-fetches cleanly under the current URLs. No-op on the first
  # call (debootstrap leaves the dir empty); load-bearing on the upstream
  # rewrite below.
  find /var/lib/apt/lists -type f -delete
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
XKBVARIANT=""
XKBOPTIONS=""

BACKSPACE="guess"
EOF

# Update the repository cache

apt_update

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

# Defer initramfs generation to the single explicit rebuild further down:
# the kernel, zfs-initramfs, and (on mirror variants) mdadm postinsts would
# otherwise each regenerate it — four builds per bake, all discarded by the
# final one. The divert survives package installs, so every postinst hits
# the /bin/true stand-in until the divert is removed.
dpkg-divert --local --rename --add /usr/sbin/update-initramfs
ln -s /bin/true /usr/sbin/update-initramfs

# Bootstrap kernel: just linux-generic. On jammy this lands the 5.15 GA
# kernel, on noble+ a 6.x. Noble+ is fine as-is for the +0:N:1 fake-root
# uidmap pattern; jammy's 5.15 + ext4 + fuse-overlayfs reports
# `Native Overlay Diff: false` and falls back to fuse-overlayfs for
# layered-image copy-up, which fails to chown character devices like
# /dev/console with `lchown ... input/output error`. The HWE kernel
# (6.8 on jammy, matches lab/pug's tracked-via-Ubuntu-HWE prod rev) is
# applied in packer/seed_deps.yml via the `hwe_kernel` role -- baked
# into box_deps, not box, so the lightweight box variant stays on the
# GA kernel for roles whose _verify exercises kernel-management
# machinery (roles/packer) or simply doesn't need HWE.
apt-get install --yes linux-generic

# Install required packages

apt-get install --yes dosfstools zfs-initramfs zfsutils-linux

# Enable systemd ZFS services

systemctl enable zfs.target
systemctl enable zfs-import-cache
systemctl enable zfs-mount
systemctl enable zfs-import.target

# Cap the ARC on small-RAM cloud VMs (hetzner cpx22 = 3.7 GB; default ARC
# of ~50% of RAM would starve headscale). Written to modprobe.d so it applies
# both at boot and inside the initramfs (initramfs-tools bundles modprobe.d),
# which matters because zfs loads from the initramfs on a root-on-ZFS host.
if [ -n "${ZFS_ARC_MAX:-}" ]; then
  echo "options zfs zfs_arc_max=${ZFS_ARC_MAX}" >/etc/modprobe.d/zfs.conf
fi

# Set ZFSBootMenu properties on datasets. The kernel cmdline carries a serial
# console so the boot log reaches qemu's -serial stdio: the harness's
# verify-boot post-processor captures it (a boot that never reaches SSH is
# otherwise a black box), and it gives headless hosts a serial getty. The
# serial console is last so it's the primary /dev/console -- kernel printk and
# the login prompt both land on serial; tty0 keeps VGA output for physical
# consoles; earlycon emits before the real driver registers its console.
#
# Source of truth for the *test-image* console args is $CONSOLE_CMDLINE, set
# in the arch case block above. Prod hosts derive theirs from boot_serial_console
# in host_vars (the boot role's "Set the base console command line" task
# overwrites this property at converge). The serial hardware differs per arch
# (8250 COM1 at io 0x3f8 vs pl011 at mmio 0x9000000), so the value is
# arch-specific. $CONSOLE_CMDLINE is used both here and in the rEFInd menuentries
# directly — no readback from ZFS needed.
zfs set org.zfsbootmenu:commandline="$CONSOLE_CMDLINE" "rpool/ROOT"

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
  udevadm settle --timeout=10
  mdadm --detail --brief /dev/md/efi >>/etc/mdadm/mdadm.conf
  EFI_DEVICE=/dev/md/efi

  # Swap on the rpool zvol provision.sh created. /dev/zvol/rpool/swap
  # is a stable udev symlink — no by-uuid translation below, and no
  # mdadm raid0 stripe (mirror rpool already gives redundancy without
  # the lose-one-disk-lose-all-swap failure mode of the raid0 layout).
  SWAP_DEVICE=/dev/zvol/rpool/swap
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

echo "$EFI_DEVICE /boot/efi vfat defaults,umask=0077 0 0" >>/etc/fstab
echo "$SWAP_DEVICE none swap discard 0 0" >>/etc/fstab

# Pull all available modules into initramfs (rather than just the
# build host's currently-loaded set) so the shipped image boots on
# bare-metal hardware whose controllers/NICs the build host didn't
# happen to have. Costs ~30 MiB qcow2-compressed. Set before the
# one-and-only build below (postinst-driven builds are diverted above).
sed -i 's/^MODULES=.*/MODULES=most/' /etc/initramfs-tools/initramfs.conf

# Restore the real update-initramfs and generate the initramfs once, now
# that MODULES=most and every package is in place. -c (not -u): the divert
# means no initramfs exists yet to update.

rm /usr/sbin/update-initramfs
dpkg-divert --local --rename --remove /usr/sbin/update-initramfs
update-initramfs -c -k all

# Mount EFI filesystem

mkdir -p /boot/efi
mount /boot/efi

# Install ZFSBootMenu
#
# ZBM is built + uploaded out-of-band by `mise run zbm:build && zbm:upload`
# (mise.toml's [vars] zbm_version drives those). The version installed
# here is decoupled — it's the $ZBM_VERSION constant set at the top of
# this script. Bump it once a new tarball has been built + uploaded to
# Nexus and verified.
#
# The tarball carries both the unified ZBM EFI image and the components-mode
# kernel + initrd. The shipped default ZBM entry uses the unified image
# (/EFI/ZBM/VMLINUZ.EFI); the aarch64 image also stages the components so the
# ZBM menu remains available even though the default boot path is the Linux
# EFI-stub entry below. rEFInd ships as refind_x64.efi on x86_64 and
# refind_aa64.efi on aarch64 ($REFIND_NAME, derived from `uname -m` above).
#
# ZBM_URL_BASE override: build VMs that cannot reach the lab Nexus (AWS
# bakes) get the tarball pre-staged into the chroot by provision.sh and
# consume it via a file:// base — curl handles both schemes identically.
ZBM_URL="${ZBM_URL_BASE:-https://nexus.lab.fahm.fr/repository/zbm}/zfsbootmenu-$ZBM_VERSION.tar.gz"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM

apt-get install --yes curl
curl -fL --retry 3 --retry-connrefused -o "$tmp/zbm.tar.gz" "$ZBM_URL"
EXPECTED_SUM="$(curl -fsSL --retry 3 --retry-connrefused "$ZBM_URL.sha256sum" | awk '{print $1}')"
echo "$EXPECTED_SUM  $tmp/zbm.tar.gz" | sha256sum -c -
tar -xzf "$tmp/zbm.tar.gz" -C "$tmp" --no-same-owner

mkdir -p /boot/efi/EFI/ZBM
mv "$tmp"/zfsbootmenu.EFI /boot/efi/EFI/ZBM/VMLINUZ.EFI
mv "$tmp"/cmdline /boot/efi/EFI/ZBM/

ZBM_CMDLINE=$(cat /boot/efi/EFI/ZBM/cmdline)

if [ "$ZBM_ARCH" = "aarch64" ]; then
  mv "$tmp"/initramfs-bootmenu.img /boot/efi/EFI/ZBM/
  mv "$tmp"/vmlinu*-bootmenu /boot/efi/EFI/ZBM/

  # x86_64 emits vmlinuz-bootmenu (compressed); aarch64 emits vmlinux-bootmenu
  # (uncompressed). Capture the actual filename for the rEFInd menuentry.
  ZBM_KERNEL="$(basename /boot/efi/EFI/ZBM/vmlin*-bootmenu)"
fi

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

# aarch64-only: stage the on-pool Linux EFI-stub kernel + initrd onto the ESP.
# The default aarch64 rEFInd entry below boots the installed Ubuntu kernel
# directly via its EFI stub, with the kernel command line carried by the
# stanza's `options` directive. The ZBM components entry remains available for
# recovery/menu access; the rEFInd -> ZBM -> kexec chain is not the default on
# this arch because it panics on EDK2/aarch64
# (notes/zbm-aarch64-kexec-bug-report.md).
#
# Wire the staging up as a kernel + initramfs hook so apt-driven kernel
# upgrades (and zfs-initramfs / similar initrd-only rebuilds) refresh
# /EFI/Linux/. The hook is the single source of truth for "pick the latest
# /boot kernel and copy it to the ESP" -- we install it first, then invoke it
# to do the initial staging. The rEFInd menuentry points at the same
# /EFI/Linux/{vmlinuz.efi,initrd} paths the hook rewrites on every kernel
# update.
if [ "$ZBM_ARCH" = "aarch64" ]; then
  # Hook script. Installed under /etc/kernel/postinst.d (fires on
  # linux-image install/upgrade) and /etc/kernel/postrm.d (fires on
  # autoremove of an old kernel — re-pick the latest remaining one).
  # Also symlinked into /etc/initramfs/post-update.d to catch
  # initrd-only rebuilds (e.g. dpkg-reconfigure zfs-initramfs) that
  # don't bump the kernel package. The script ignores its arguments
  # and always restages the highest-versioned kernel + initrd from
  # /boot, matching the selection rule used during the build.
  #
  # `zz-` prefix puts us after initramfs-tools' own postinst.d hook
  # so the new initrd is on disk before we copy it. Atomic rename
  # (.new + mv) prevents a power loss mid-write from leaving a
  # half-written kernel image on the ESP.
  cat <<'HOOK' >/etc/kernel/postinst.d/zz-stage-efi-stub
#!/bin/bash
set -euo pipefail

mountpoint -q /boot/efi || exit 0

shopt -s nullglob
vmlinuz_files=(/boot/vmlinuz-*)
initrd_files=(/boot/initrd.img-*)
shopt -u nullglob

[ ${#vmlinuz_files[@]} -gt 0 ] || exit 0
[ ${#initrd_files[@]} -gt 0 ] || exit 0

vmlinuz=$(printf '%s\n' "${vmlinuz_files[@]}" | sort -V | tail -1)
initrd=$(printf '%s\n' "${initrd_files[@]}" | sort -V | tail -1)

mkdir -p /boot/efi/EFI/Linux

# jammy/noble ship vmlinuz-* gzipped (dual-format ARM64-Image+PE);
# EDK2's LoadImage doesn't decompress gzip, so materialise the
# underlying Image. resolute ships an uncompressed PE32+ EFI stub
# directly. `gunzip -t` is a cheap format probe.
if gunzip -t "$vmlinuz" 2>/dev/null; then
  gunzip -c "$vmlinuz" >/boot/efi/EFI/Linux/vmlinuz.efi.new
else
  cp -L "$vmlinuz" /boot/efi/EFI/Linux/vmlinuz.efi.new
fi
if cmp -s /boot/efi/EFI/Linux/vmlinuz.efi.new /boot/efi/EFI/Linux/vmlinuz.efi; then
  rm /boot/efi/EFI/Linux/vmlinuz.efi.new
else
  mv -f /boot/efi/EFI/Linux/vmlinuz.efi.new /boot/efi/EFI/Linux/vmlinuz.efi
fi

cp -L "$initrd" /boot/efi/EFI/Linux/initrd.new
if cmp -s /boot/efi/EFI/Linux/initrd.new /boot/efi/EFI/Linux/initrd; then
  rm /boot/efi/EFI/Linux/initrd.new
else
  mv -f /boot/efi/EFI/Linux/initrd.new /boot/efi/EFI/Linux/initrd
fi
HOOK
  chmod +x /etc/kernel/postinst.d/zz-stage-efi-stub

  mkdir -p /etc/kernel/postrm.d /etc/initramfs/post-update.d
  ln -sf ../postinst.d/zz-stage-efi-stub /etc/kernel/postrm.d/zz-stage-efi-stub
  ln -sf /etc/kernel/postinst.d/zz-stage-efi-stub \
    /etc/initramfs/post-update.d/zz-stage-efi-stub

  # Initial staging — same code path as every subsequent kernel
  # upgrade will take.
  /etc/kernel/postinst.d/zz-stage-efi-stub
fi

if [ "$ZBM_ARCH" = "aarch64" ]; then
  refind_default_selection="Ubuntu (Linux EFI Stub)"
  refind_dont_scan_dirs="EFI:/EFI/ZBM,EFI:/EFI/Linux"
else
  refind_default_selection="Ubuntu (ZBM)"
  refind_dont_scan_dirs="EFI:/EFI/ZBM"
fi

cat <<EOF >/boot/efi/EFI/refind/refind.conf
timeout 3
default_selection "$refind_default_selection"
dont_scan_dirs $refind_dont_scan_dirs

# $CONSOLE_CMDLINE is passed explicitly to every ZBM stage so the rEFInd ->
# ZBM -> kexec handoff narrates itself on the serial console. A stalled boot
# (e.g. the intermittent first-boot SSH-bringup flake the test harness hits)
# is only diagnosable if that stage is visible. zbm.skip still bypasses the
# menu so the boot stays non-interactive.
#
# Twin of the converge-time roles/refind/templates/refind.conf.j2, kept in sync by
# hand.
menuentry "Ubuntu (ZBM)" {
    loader /EFI/ZBM/VMLINUZ.EFI
    options "$ZBM_CMDLINE $CONSOLE_CMDLINE zbm.skip"
    submenuentry "Show ZFSBootMenu" {
      options "$ZBM_CMDLINE $CONSOLE_CMDLINE zbm.show"
    }
}
EOF

if [ "$ZBM_ARCH" = "aarch64" ]; then

  cat <<EOF >>/boot/efi/EFI/refind/refind.conf
menuentry "Ubuntu (ZBM, Components)" {
    loader /EFI/ZBM/${ZBM_KERNEL}
    initrd /EFI/ZBM/initramfs-bootmenu.img
    options "$ZBM_CMDLINE $CONSOLE_CMDLINE zbm.skip"
    submenuentry "Show ZFSBootMenu" {
      options "$ZBM_CMDLINE $CONSOLE_CMDLINE zbm.show"
    }
}

menuentry "Ubuntu (Linux EFI Stub)" {
    loader /EFI/Linux/vmlinuz.efi
    initrd /EFI/Linux/initrd
    options "root=zfs:rpool/ROOT/${UBUNTU_NAME} $(zfs get -H -o value org.zfsbootmenu:commandline rpool/ROOT)"
}
EOF

fi

# Configure EFI boot entries. rEFInd is the firmware entry for the image. On
# aarch64, the Linux EFI-stub boot path is the manual rEFInd menuentry above;
# its kernel command line lives in refind.conf `options`.

apt-get install --yes efibootmgr

# On the multi-disk mdadm-EFI mirror, register one boot entry per disk
# so the system survives losing any single disk — firmware only follows
# paths it knows about, and an entry is per-disk regardless of whether
# the ESP content is mirrored. Single-disk variants get a single bare
# "rEFInd" entry (unchanged).
#
# -p 2 is the ESP: provision.sh's layout is 1=bios(EF02), 2=efi(EF00),
# 3=swap/meta, 4=rpool. Keep these efibootmgr -p values in sync with that
# order -- a stale -p (e.g. 1, the BIOS-boot partition) registers a boot
# entry the firmware can't load.
if [ "$DISKS_COUNT" -eq 1 ]; then
  efibootmgr -c -d "$DISKS" -p 2 \
    -L "rEFInd" \
    -l "\\EFI\\refind\\${REFIND_NAME}"
else
  idx=0
  # shellcheck disable=SC2086  # word-splitting on DISKS is the point
  for disk in $DISKS; do
    efibootmgr -c -d "$disk" -p 2 \
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

# User setup. The qemu fixtures bake a key-only `vagrant` sudoer so the test
# harness can SSH back in. The hetzner image must NOT bake a login user (the
# snapshot would ship a known key on the one internet-facing host); instead it
# installs cloud-init so terraform's user_data creates `ak` + injects the SSH
# key on first boot, exactly as the stock hcloud image does.
if [ "${IMAGE_TARGET:-qemu}" = "hetzner" ]; then
  # cloud-guest-utils ships growpart, used by hetzner_growpart.service below.
  apt-get install --yes cloud-init cloud-guest-utils

  # Pin the datasource so a fresh cloud-init (debootstrap'd, not the
  # Hetzner-tuned stock image) finds Hetzner's metadata + user-data fast
  # instead of probing the full list. Hetzner provides networking + user-data
  # here, so cloud-init owns the netplan (provision.sh skipped its static one).
  # VALIDATE on a throwaway cpx22: confirm `ak` is created and SSH works — if
  # the Hetzner DS isn't detected (DMI mismatch), fall back to ConfigDrive/
  # NoCloud or force ds=hetzner on the kernel cmdline. See notes.
  cat <<EOF >/etc/cloud/cloud.cfg.d/99-hetzner.cfg
datasource_list: [ Hetzner, ConfigDrive, NoCloud, None ]
EOF

  # Image ships at 20G but deploys onto cpx22's ~76G, leaving rpool's partition
  # (p4, last on disk) short with the GPT backup header mid-disk.
  # hetzner_growpart.service grows p4 (growpart relocates the backup header) and
  # runs `zpool online -e` once on first boot — late + sentinel-gated so a
  # failure can't wedge the root mount. autoexpand covers any later disk resize.
  zpool set autoexpand=on rpool

  cat <<'EOF' >/usr/local/sbin/hetzner_growpart.sh
#!/bin/bash
set -euo pipefail

disk=/dev/sda
part=4

rc=0
growpart "$disk" "$part" || rc=$?
# growpart: 0 = resized, 1 = NOCHANGE (already full), >1 = real error.
if [ "$rc" -gt 1 ]; then
  exit "$rc"
fi

udevadm settle || true
zpool online -e rpool "${disk}${part}"
EOF
  chmod 0755 /usr/local/sbin/hetzner_growpart.sh

  cat <<'EOF' >/etc/systemd/system/hetzner_growpart.service
[Unit]
Description=Grow rpool into the rest of the boot disk on first boot
After=zfs.target
ConditionPathExists=!/var/lib/hetzner_growpart.done

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/hetzner_growpart.sh
ExecStartPost=/usr/bin/touch /var/lib/hetzner_growpart.done

[Install]
WantedBy=multi-user.target
EOF
  systemctl enable hetzner_growpart.service

else

  # Configure networking. Match by name glob so the same image works
  # under any qemu device topology (packer's vs. testrole's direct-kernel
  # boot give the NIC different kernel names — ens3/ens4/etc.) and on
  # baremetal (eno1/enp0s31f6/...). All Predictable Network Interface
  # Names start with "en"; only old-style "eth*" is excluded, which
  # requires net.ifnames=0 on modern Ubuntu and so is essentially extinct.
  #
  # Multi-NIC hosts: this stanza claims every "en*" interface as
  # "primary", so each onboard NIC will DHCP independently. Bonded /
  # LACP setups need bare-metal callers to overwrite this file with an
  # explicit netplan before first boot.
  #
  # Skipped on hetzner: cloud-init (configured in chroot.sh for the Hetzner
  # datasource) owns networking there, exactly as the stock hcloud image does —
  # a competing static netplan here would fight cloud-init's generated one.
  cat <<EOF >/etc/netplan/01-netcfg.yaml
network:
  version: 2
  ethernets:
    primary:
      match:
        name: "en*"
      dhcp4: true
      dhcp-identifier: mac
EOF

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
fi

# Reset apt sources to upstream so the shipped image isn't pinned to a
# Nexus-internal URL. Build-time installs above used $UBUNTU_MIRROR
# (Nexus by default); ansible's mirror_apt_ubuntu_* may rewrite this
# again on first run, but the at-rest image must point at canonical
# Ubuntu mirrors.
write_sources_list "$UBUNTU_MIRROR_UPSTREAM" "$UBUNTU_MIRROR_SECURITY_UPSTREAM"

# Refresh /var/lib/apt/lists/ under the upstream URLs (write_sources_list
# just cleared the build-time Nexus lists) so the shipped image carries a
# coherent cache: a role doing `apt: pkg:` with cache_valid_time set
# (hwe_kernel in packer/seed_deps.yml) skips its own update and would
# otherwise find no candidate.
apt_update

# Drop the downloaded .deb cache (build-only, ~hundreds of MB) so it doesn't
# ride into every deployment. Clears /var/cache/apt/archives only — the
# lists/ repopulated just above stay intact.
apt-get clean

# Blank machine-id so systemd regenerates a unique one on first boot —
# otherwise every host from this snapshot shares one (journald, systemd
# instance ids, DHCP DUID). Re-point dbus's copy at it when present.
: >/etc/machine-id
if [ -e /var/lib/dbus/machine-id ]; then
  ln -sf /etc/machine-id /var/lib/dbus/machine-id
fi

# Drop build-time logs (dpkg/apt/debootstrap) so the image starts clean.
# Files only — keep the dir tree services expect. Last, to catch the
# writes from the steps just above.
find /var/log -type f -delete
