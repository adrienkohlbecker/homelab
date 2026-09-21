#!/bin/bash
set -euxo pipefail

# Package postinsts must not start services inside the incomplete target root.
# The script explicitly enables the units the shipped image needs after their
# configuration is in place; remove the temporary policy when the build exits.
printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d
chmod 0755 /usr/sbin/policy-rc.d

cleanup() {
  rm -f /usr/sbin/policy-rc.d
  if [ -n "${tmp:-}" ]; then
    rm -rf "$tmp"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# Env consumed by this script:
# - From packer's shell-provisioner env block (qemu.pkr.hcl):
#   UBUNTU_NAME, UBUNTU_MIRROR, UBUNTU_MIRROR_SECURITY,
#   UBUNTU_MIRROR_UPSTREAM, UBUNTU_MIRROR_SECURITY_UPSTREAM,
#   SSH_KEY_PUB, ZBM_VERSION, and optionally REFIND_DEB_URL/REFIND_DEB_SHA256
#   (a pinned rEFInd package; empty installs the distribution's).
# - Inherited from provision.sh: DISKS, LAYOUT, CHROOT_ROLE_FILES, INSTALL_TARGET,
#   PARTITIONS_EFI, PARTITIONS_SWAP, PARTITIONS_PODMAN,
#   HOSTNAME, USERNAME.
#   PARTITIONS_EFI/SWAP are always set; on a mirror they are mdadm'd into
#   /dev/md/efi (raid1) and /dev/md/swap (raid1). PARTITIONS_PODMAN is set
#   when PODMAN_SIZE is (raid5 /dev/md/podman on a mirror).
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
  SERIAL_CMDLINE="earlycon=uart8250,io,0x3f8 console=ttyS0,115200"
  ;;
aarch64)
  REFIND_NAME=refind_aa64.efi
  REFIND_FALLBACK_NAME=BOOTAA64.EFI
  SERIAL_CMDLINE="earlycon=pl011,0x9000000,115200 console=ttyAMA0,115200"
  ;;
*)
  echo >&2 "Unsupported arch: $ZBM_ARCH"
  exit 1
  ;;
esac

DISKS_COUNT=$(wc -w <<<"$DISKS")

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

# apt already retries transient fetch failures (Nexus restart, packet loss)
# three times with backoff by default on every release we build, so the new
# install needs no drop-in for the per-file case. apt_update below is the
# coarse absorb for a restart that outlasts those retries.

# apt-get update exits 0 even when one component's Packages index fails to
# download (Nexus restart, dropped packet), leaving a partial cache that makes
# a later install fail with a baffling "Unable to locate package". Error-Mode
# =any turns a failed fetch into a non-zero exit; the loop retries with backoff
# so a brief blip is absorbed. Same helper as provision.sh's build-VM apt.
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
# URLs regardless of build-time routing. Supported releases use deb822
# .sources files, matching both the stock Noble layout and what roles/apt
# converges to.
write_sources_list() {
  # Shape matches what roles/apt's deb822_repository tasks render (the
  # module emits fields sorted by parameter name), so a diff between the
  # packer-baked files and the post-apply state highlights real drift
  # (mirror substitution) rather than format noise. Stock noble ships a
  # single ubuntu.sources; the archive/security split mirrors roles/apt,
  # which overwrites ubuntu.sources and adds ubuntu-security.sources on
  # first apply.
  local deb_arch
  if [ "$ZBM_ARCH" = "aarch64" ]; then
    deb_arch=arm64
  else
    deb_arch=amd64
  fi

  truncate -s0 /etc/apt/sources.list
  mkdir -p /etc/apt/sources.list.d

  cat <<EOF >/etc/apt/sources.list.d/ubuntu.sources
Architectures: $deb_arch
Components: main universe restricted multiverse
Languages: none
X-Repolib-Name: ubuntu
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
Suites: $UBUNTU_NAME $UBUNTU_NAME-updates $UBUNTU_NAME-backports
Types: deb
URIs: $1
EOF

  cat <<EOF >/etc/apt/sources.list.d/ubuntu-security.sources
Architectures: $deb_arch
Components: main universe restricted multiverse
Languages: none
X-Repolib-Name: ubuntu-security
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
Suites: $UBUNTU_NAME-security
Types: deb
URIs: $2
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

# Install the same console policy the console role owns. These files are static,
# so the chroot can consume them directly without a template renderer.
install -m 0644 \
  "${CHROOT_ROLE_FILES}/console-setup" \
  /etc/default/console-setup
install -m 0644 \
  "${CHROOT_ROLE_FILES}/keyboard" \
  /etc/default/keyboard

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

# Bootstrap the release GA kernel.
apt-get install --yes linux-generic

# Install required packages

apt-get install --yes curl dosfstools zfs-initramfs zfsutils-linux

# Enable systemd ZFS services

systemctl enable zfs.target
systemctl enable zfs-import-cache
systemctl enable zfs-mount
systemctl enable zfs-import.target

# Cap the ARC on small-RAM cloud VMs (hetzner cpx22 = 3.7 GB; default ARC
# of ~50% of RAM would starve headscale). Written to modprobe.d so it applies
# both at boot and inside the initramfs (initramfs-tools bundles modprobe.d),
# which matters because zfs loads from the initramfs on a root-on-ZFS host.
if [ "${ZFS_ARC_MAX:-0}" != "0" ]; then
  echo "options zfs zfs_arc_max=${ZFS_ARC_MAX}" >/etc/modprobe.d/zfs.conf
fi

# Keep VGA output on every target. QEMU adds the architecture-specific serial
# console consumed by verify-boot and disables mitigations only in disposable
# nested-KVM cells. The fragment preserves that tuning when the boot role
# rebuilds the command line during a test. Hetzner exposes a serial console,
# listed before tty0 so the VGA console keeps /dev/console; bare-metal lab and
# pug have no UART. The boot role owns the final prod command line.
COMMANDLINE="console=tty0"

if [ "$INSTALL_TARGET" = "qemu" ]; then
  COMMANDLINE="$COMMANDLINE $SERIAL_CMDLINE mitigations=off"
  mkdir -p /etc/zfsbootmenu
  echo "mitigations=off" >/etc/zfsbootmenu/mitigations
elif [ "$INSTALL_TARGET" = "hetzner" ]; then
  COMMANDLINE="$SERIAL_CMDLINE $COMMANDLINE"
fi

zfs set org.zfsbootmenu:commandline="$COMMANDLINE" "rpool/ROOT"

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

  # Image builds write to fresh qcow2s that read back as zeros, and all-zero
  # members are already consistent: RAID1 halves match and RAID5 parity of
  # zeros is zero. Skip the initial resync rather than wait on it before
  # sealing. Bare metal keeps it -- its disks hold arbitrary old data.
  MDADM_CREATE_OPTS=()
  if [ "$INSTALL_TARGET" != bare_metal ]; then
    MDADM_CREATE_OPTS+=(--assume-clean)
  fi

  # Metadata 1.0 and no bitmap keep each RAID1 member recognizable as an ESP.
  # Some firmware may still reject a member; per-disk NVRAM entries below retain
  # alternate boot paths. Validate this layout on each new bare-metal platform.
  # shellcheck disable=SC2086  # word-splitting on PARTITIONS_EFI is the point
  mdadm --create "${MDADM_CREATE_OPTS[@]}" /dev/md/efi --name=efi --metadata=1.0 --level="raid1" --bitmap=none --raid-devices="$DISKS_COUNT" $PARTITIONS_EFI
  udevadm settle --timeout=10
  mdadm --detail --brief /dev/md/efi >>/etc/mdadm/mdadm.conf
  EFI_DEVICE=/dev/md/efi

  # Disk-backed RAID1 swap avoids the memory-pressure deadlock risk of a ZFS
  # zvol. mdadm.conf makes it available to the initramfs and swapon at boot.
  # shellcheck disable=SC2086  # word-splitting on PARTITIONS_SWAP is the point
  mdadm --create "${MDADM_CREATE_OPTS[@]}" /dev/md/swap --name=swap --metadata=1.2 --level=raid1 --bitmap=none --raid-devices="$DISKS_COUNT" $PARTITIONS_SWAP
  udevadm settle --timeout=10
  mdadm --detail --brief /dev/md/swap >>/etc/mdadm/mdadm.conf
  SWAP_DEVICE=/dev/md/swap

  # The reconstructible Podman store uses RAID5 without a write-intent bitmap:
  # accept a full replacement resync in exchange for lower write amplification.
  # Record the array for boot-time assembly; the Podman role formats and mounts
  # /dev/md/podman.
  if [ -n "$PARTITIONS_PODMAN" ]; then
    # shellcheck disable=SC2086  # word-splitting on PARTITIONS_PODMAN is the point
    mdadm --create "${MDADM_CREATE_OPTS[@]}" /dev/md/podman --force --name=podman --metadata=1.2 --level=raid5 --bitmap=none --raid-devices="$DISKS_COUNT" $PARTITIONS_PODMAN
    udevadm settle --timeout=10
    mdadm --detail --brief /dev/md/podman >>/etc/mdadm/mdadm.conf
  fi
fi

# Create filesystems

mkdosfs -F 32 -s 1 -n EFI "$EFI_DEVICE"
mkswap -f "$SWAP_DEVICE"

# UUIDs exist only after the filesystems above have been created; blkid
# reads the signatures back through the same page cache they were written
# with, so no settle is needed.
if [ "$LAYOUT" = "" ]; then
  EFI_DEVICE="/dev/disk/by-uuid/$(blkid -s UUID -o value "$EFI_DEVICE")"
  SWAP_DEVICE="/dev/disk/by-uuid/$(blkid -s UUID -o value "$SWAP_DEVICE")"
fi

# Update fstab

# Keep the ESP mount policy in sync with boot_esp_mount_opts in
# group_vars/all/main.yml.
echo "$EFI_DEVICE /boot/efi vfat defaults,umask=0077 0 0" >>/etc/fstab
echo "$SWAP_DEVICE none swap discard 0 0" >>/etc/fstab

# Pull all available modules into initramfs (rather than just the build host's
# currently-loaded set) so the shipped image boots on bare-metal hardware whose
# controllers/NICs the builder didn't have. Install the boot role's canonical
# conf.d file before the one-and-only build below.
install -m 0644 \
  "${CHROOT_ROLE_FILES}/modules_most" \
  /etc/initramfs-tools/conf.d/modules-most

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
# ZBM is built + published out-of-band by `mise run zbm:build && zbm:upload`.
# qemu.pkr.hcl reads the architecture-specific release from
# group_vars/all/versions.yml and passes it here as $ZBM_VERSION.
#
# The tarball carries both the unified ZBM EFI image and the components-mode
# kernel + initrd. The default ZBM entry uses the unified image
# (/EFI/ZBM/VMLINUZ.EFI); the aarch64 image also stages the components as a
# recovery entry. rEFInd ships as refind_x64.efi on x86_64 and refind_aa64.efi
# on aarch64 ($REFIND_NAME, derived from `uname -m` above).
#
# The registry path is project 83079143 = akohlbecker/homelab (numeric id
# keeps the path free of an encoded slash); the project is public, so the
# pull is anonymous.
ZBM_URL="https://gitlab.com/api/v4/projects/83079143/packages/generic/zfsbootmenu/$ZBM_VERSION/zfsbootmenu-$ZBM_VERSION.tar.gz"

tmp=$(mktemp -d)

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

# Noble's rEFInd 0.13.2 wedges the second boot under edk2-stable202408; the
# packer template passes the pinned newer package only for releases that need it.
if [ -n "${REFIND_DEB_URL:-}" ]; then
  curl -fL --retry 3 --retry-connrefused -o /tmp/refind.deb "$REFIND_DEB_URL"
  echo "$REFIND_DEB_SHA256  /tmp/refind.deb" | sha256sum -c -
  apt-get install --yes /tmp/refind.deb
  rm /tmp/refind.deb
else
  apt-get install --yes refind
fi
refind-install
rm /boot/refind_linux.conf

# Drop a copy at the firmware fallback path (\EFI\BOOT\BOOT<arch>.EFI)
# so a host whose NVRAM has been wiped (CMOS clear, BIOS update,
# "Restore Defaults") still boots from the ESP. refind-install does
# not write here by default on Debian/Ubuntu.
mkdir -p /boot/efi/EFI/BOOT
cp "/boot/efi/EFI/refind/$REFIND_NAME" "/boot/efi/EFI/BOOT/$REFIND_FALLBACK_NAME"

# Menu countdown. 3 matches the role template (a converge overwrites this
# file with that value).

cat <<EOF >/boot/efi/EFI/refind/refind.conf
timeout 3
default_selection "Ubuntu (ZBM)"
dont_scan_dirs EFI:/EFI/ZBM

# Twin of the converge-time roles/refind/templates/refind.conf.j2, kept in sync by
# hand.
menuentry "Ubuntu (ZBM)" {
    loader /EFI/ZBM/VMLINUZ.EFI
    options "$ZBM_CMDLINE $COMMANDLINE zbm.skip"
    submenuentry "Show ZFSBootMenu" {
      options "$ZBM_CMDLINE $COMMANDLINE zbm.show"
    }
}
EOF

if [ "$ZBM_ARCH" = "aarch64" ]; then

  cat <<EOF >>/boot/efi/EFI/refind/refind.conf
menuentry "Ubuntu (ZBM, Components)" {
    loader /EFI/ZBM/${ZBM_KERNEL}
    initrd /EFI/ZBM/initramfs-bootmenu.img
    options "$ZBM_CMDLINE $COMMANDLINE zbm.skip"
    submenuentry "Show ZFSBootMenu" {
      options "$ZBM_CMDLINE $COMMANDLINE zbm.show"
    }
}
EOF

fi

# Mirror the config next to the fallback binary. rEFInd only reads
# refind.conf from its own directory, so the fallback copy at \EFI\BOOT
# otherwise runs config-less: 20s default countdown, then the first
# auto-scanned loader (the ZBM image) with empty load options — no zbm.skip
# (ZBM's own menu wait) and no serial console args (silent boot). That is
# the normal boot path on EC2 cells, whose UEFI NVRAM starts empty (boot
# entries do not ride an AMI); measured at ~26s of pure countdown per boot.
# qemu fixtures boot via the baked NVRAM entry (the harness reuses
# efivars.fd) and never read this copy.
cp /boot/efi/EFI/refind/refind.conf /boot/efi/EFI/BOOT/refind.conf

# Configure EFI boot entries. rEFInd is the firmware entry for the image; the
# kernel command line lives in refind.conf `options`.

# On the multi-disk mdadm-EFI mirror, register one boot entry per disk
# so the system survives losing any single disk — firmware only follows
# paths it knows about, and an entry is per-disk regardless of whether
# the ESP content is mirrored. Single-disk variants get a single bare
# "rEFInd" entry (unchanged).
#
# -p 2 is the ESP: provision.sh's layout is 1=bios(EF02), 2=efi(EF00),
# 3=swap, 4=podman, 5=rpool, 6=meta. Keep these efibootmgr -p values in sync
# with that order -- a stale -p (e.g. 1, the BIOS-boot partition) registers a
# boot entry the firmware can't load.
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

# Enable tmp mount. Noble ships tmp.mount as a template under
# /usr/share/systemd/ and leave it disabled — copy + enable. resolute's
# systemd ships /usr/lib/systemd/system/tmp.mount and pre-symlinks it into
# local-fs.target.wants/, so it's enabled out of the box; skip the copy.

if [ -f /usr/share/systemd/tmp.mount ]; then
  cp /usr/share/systemd/tmp.mount /etc/systemd/system/
  systemctl enable tmp.mount
fi

# Add more packages

apt-get install --yes openssh-server

# qemu-guest-agent is a KVM guest<->host channel (graceful shutdown, IP
# reporting) that binds the virtio-serial port /dev/virtio-ports/org.qemu.
# guest_agent.0; on bare metal that port never appears and the service sits
# inert. Install it only on the virtualized qemu and Hetzner targets.
if [ "$INSTALL_TARGET" != bare_metal ]; then
  apt-get install --yes qemu-guest-agent
fi

# Disable ssh password authentication. The vagrant account is
# key-only (no password set, see below); other users on the image
# don't exist. Drop a snippet under sshd_config.d so the override
# wins even if /etc/ssh/sshd_config is later edited.
echo 'PasswordAuthentication no' >/etc/ssh/sshd_config.d/00-hardening.conf

# User setup. The qemu fixtures bake a key-only `vagrant` sudoer so the test
# harness can SSH back in. The hetzner image must NOT bake a login user (the
# snapshot would ship a known key on the one internet-facing host); instead it
# installs cloud-init so terraform's user_data creates `ak` + injects the SSH
# key on first boot, exactly as the stock hcloud image does.
if [ "$INSTALL_TARGET" = "hetzner" ]; then
  bash /var/tmp/hetzner/install.sh
  rm -rf /var/tmp/hetzner

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
  # Skipped on hetzner: cloud-init (configured by packer/hetzner/install.sh)
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

# Mirror the journal onto the virtio console the harness always attaches. A
# guest that never reaches SSH -- a broken NIC backend, a wedged boot -- then
# still explains itself in an artifact, and the stream survives the reboots
# that wipe the fixture's volatile journal.
#
# journalctl --follow and not journald's own ForwardToConsole: forwarding
# starts only once journald opens the console, drops everything logged before
# that, and never replays the kernel records it imported from /dev/kmsg, so
# the artifact opened mid-boot with no kernel lines at all. --lines=all
# replays the journal from its first entry, so a late start costs nothing and
# the mirror begins where the boot does. It also buys journalctl's formatting
# -- ISO timestamps with an offset (the guest runs UTC, the harness rarely
# does), hostname, and priority colour -- which ForwardToConsole hardcodes
# away behind a bare monotonic counter.
#
# --cursor-file keeps a Restart= from replaying the whole journal again; /run
# scopes it to the boot, which is also the lifetime of the volatile journal.
if [ "$INSTALL_TARGET" = "qemu" ]; then
  cat <<'UNIT' >/etc/systemd/system/homelab_guest_journal.service
[Unit]
Description=Mirror the journal to the harness virtio console
DefaultDependencies=no
After=systemd-journald.service
Before=sysinit.target
# No start rate limit: journald may not have a readable journal on the first
# attempt this early in boot, and a burst of quick exits must not retire the
# unit for the rest of the run. StartLimitIntervalSec is a [Unit] key -- in
# [Service] systemd only warns, and that warning then fails every later
# `systemd-analyze verify` the systemd_unit helper runs.
StartLimitIntervalSec=0

[Service]
Type=exec
Environment=SYSTEMD_COLORS=true
ExecStart=/usr/bin/journalctl --follow --lines=all --output=short-iso-precise --cursor-file=/run/homelab_guest_journal.cursor
StandardOutput=file:/dev/hvc0
StandardError=null
Restart=always
RestartSec=1
OOMScoreAdjust=-900

[Install]
WantedBy=sysinit.target
UNIT
  # verify exits 0 on "Unknown key name ... ignoring", so a misplaced
  # directive passes the bake and then fails every unit the systemd_unit
  # helper validates on the fixture. Treat any complaint as fatal here.
  guest_journal_verify=$(systemd-analyze verify /etc/systemd/system/homelab_guest_journal.service 2>&1)
  if [ -n "$guest_journal_verify" ]; then
    echo "homelab_guest_journal.service did not verify cleanly:" >&2
    echo "$guest_journal_verify" >&2
    exit 1
  fi
  systemctl enable homelab_guest_journal.service
  # systemd-getty-generator puts a getty on every virtualization console it
  # knows, hvc0 included. Nothing ever types at ours -- the chardev is a
  # write-only file -- so the getty only interleaves login banners into the
  # artifact. Masked by instance name: the generator synthesizes the unit at
  # runtime, so the list-unit-files guard used for the masks below misses it.
  systemctl mask serial-getty@hvc0.service
fi

# Prevent background apt work from taking the dpkg lock in QEMU cells. The
# unattended_upgrades role unmasks its timers; the boot role owns the multipath
# masks. Only mask installed units so the image carries no dangling symlinks.
if [ "$INSTALL_TARGET" = "qemu" ]; then
  for unit in apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service \
    multipathd.service multipathd.socket; do
    if systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q .; then
      systemctl mask "$unit"
    fi
  done
fi

# Reset apt sources to upstream so the shipped image isn't pinned to a
# Nexus-internal URL. Build-time installs above used $UBUNTU_MIRROR
# (Nexus by default); ansible's mirror_apt_ubuntu_* may rewrite this
# again on first run, but the at-rest image must point at canonical
# Ubuntu mirrors.
write_sources_list "$UBUNTU_MIRROR_UPSTREAM" "$UBUNTU_MIRROR_SECURITY_UPSTREAM"

# Refresh /var/lib/apt/lists/ under the upstream URLs (write_sources_list
# just cleared the build-time Nexus lists) so the shipped image carries a
# coherent cache: package tasks using cache_valid_time may skip their own update
# and would otherwise find no candidate.
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
