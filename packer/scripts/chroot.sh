#!/bin/bash
set -euxo pipefail

# Installs and configures the root it runs in. provision.sh, which runs it, has
# already set up the storage and written the root's storage and identity
# wiring: fstab, mdadm.conf on a mirror, netplan (cloud-init's job on Hetzner),
# and hostid. The environment it reads, exported by provision.sh: UBUNTU_NAME,
# INSTALL_TARGET, HOSTNAME, USERNAME, SSH_KEY_PUB, CHROOT_FILES (the directory
# holding this script and the files it installs), the UBUNTU_MIRROR* build and
# upstream URLs, and, for the boot loader, DISKS, COMMANDLINE (the kernel
# command line for rEFInd's options), ZBM_VERSION, and optionally
# REFIND_DEB_URL/REFIND_DEB_SHA256 (a pinned rEFInd package; empty installs the
# distribution's).
# All list-shaped vars are space-delimited strings (bash arrays don't
# survive `export`); use them unquoted to word-split.

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

# Twin of provision.sh's apt_update; see the comment there.
apt_update() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if apt-get update --error-on=any; then
      return 0
    fi
    echo "apt-get update attempt ${attempt} failed; retrying in $((attempt * 5))s" >&2
    sleep "$((attempt * 5))"
  done
  echo "apt-get update failed after 5 attempts" >&2
  return 1
}

# Twin of provision.sh's write_sources_list; see the comments there.
write_sources_list() {
  local deb_arch
  deb_arch=$(dpkg --print-architecture)

  truncate -s0 /etc/apt/sources.list
  mkdir -p /etc/apt/sources.list.d

  cat <<EOF >/etc/apt/sources.list.d/ubuntu.sources
Architectures: $deb_arch
Components: main universe restricted multiverse
Languages: none
X-Repolib-Name: ubuntu
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
Suites: $1 $1-updates $1-backports
Types: deb
URIs: $2
EOF

  cat <<EOF >/etc/apt/sources.list.d/ubuntu-security.sources
Architectures: $deb_arch
Components: main universe restricted multiverse
Languages: none
X-Repolib-Name: ubuntu-security
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
Suites: $1-security
Types: deb
URIs: $3
EOF

  find /var/lib/apt/lists -type f -delete
}

# Arch-derived constants: rEFInd EFI binary names + ZBM tarball arch
# token. The build VM and the shipped image are always the same arch,
# so detecting via `uname -m` here is equivalent to passing in from
# packer. Fail loud on unsupported arches; a new arch also needs its
# entry in data/architectures.yml.
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

# Build-time mirror first ($UBUNTU_MIRROR, Nexus by default); the upstream
# pair replaces it at the very end.
write_sources_list "$UBUNTU_NAME" "$UBUNTU_MIRROR" "$UBUNTU_MIRROR_SECURITY"

# Configure locale

locale-gen en_US.UTF-8
update-locale --reset LANG=en_US.UTF-8

# Configure timezone

ln -fs /usr/share/zoneinfo/Etc/UTC /etc/localtime

# Install the same console policy the console role owns. These files are static,
# so the chroot can consume them directly without a template renderer.
install -m 0644 \
  "${CHROOT_FILES}/console-setup" \
  /etc/default/console-setup
install -m 0644 \
  "${CHROOT_FILES}/keyboard" \
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

# Defer image generation until the storage and console configuration is ready.
# Dracut kernel hooks invoke dracut directly, while initramfs-tools hooks use
# update-initramfs; divert the selected backend so every postinst is deferred.
# Twin of initramfs_use_dracut in group_vars/all/main.yml: every release after
# Noble uses Dracut.
case "$UBUNTU_NAME" in
noble)
  initramfs_generator=initramfs-tools
  initramfs_command=/usr/sbin/update-initramfs
  zfs_initramfs_package=zfs-initramfs
  initramfs_policy_source="${CHROOT_FILES}/modules_most"
  initramfs_policy_target=/etc/initramfs-tools/conf.d/modules-most
  # -c because the diverted package hooks have not created any images yet.
  initramfs_build=(update-initramfs -c -k all)
  ;;
*)
  initramfs_generator=dracut
  initramfs_command=/usr/bin/dracut
  zfs_initramfs_package=zfs-dracut
  initramfs_policy_source="${CHROOT_FILES}/dracut_host.conf"
  initramfs_policy_target=/etc/dracut.conf.d/90_host.conf
  # The installer chroot cannot detect the booted target hardware. The first
  # image is generic; later on-host builds use the host-only policy.
  initramfs_build=(dracut --no-hostonly --force --regenerate-all)
  ;;
esac
dpkg-divert --local --rename --add "$initramfs_command"
ln -s /bin/true "$initramfs_command"

# Bootstrap the release GA kernel.
apt-get install --yes "$initramfs_generator" linux-generic

# Install required packages

apt-get install --yes curl dosfstools "$zfs_initramfs_package" zfsutils-linux

# QEMU fixtures disable mitigations in disposable nested-KVM cells; this
# fragment preserves that when the boot role rebuilds the command line during a
# test.
if [ "$INSTALL_TARGET" = "qemu" ]; then
  mkdir -p /etc/zfsbootmenu
  echo "mitigations=off" >/etc/zfsbootmenu/mitigations
fi

# The installer wrote this root's fstab and, on a mirror, its mdadm.conf.
# mdadm assembles the listed arrays at boot, so it goes into the image built
# below. efibootmgr manages the firmware entries rEFInd is registered with.
if grep -q '^ARRAY' /etc/mdadm/mdadm.conf 2>/dev/null; then
  apt-get install --yes mdadm
fi
apt-get install --yes efibootmgr

# Install the boot role image policy before the single final build.
install -Dm 0644 "$initramfs_policy_source" "$initramfs_policy_target"

# Restore the backend and create images for every installed kernel.

rm "$initramfs_command"
dpkg-divert --local --rename --remove "$initramfs_command"
"${initramfs_build[@]}"

mkdir -p /boot/efi

# Put ZBM and rEFInd on the ESP and register rEFInd with the firmware.
install_boot_loader() {
  local disks_count
  disks_count=$(wc -w <<<"$DISKS")

  mount /boot/efi

  # Install ZFSBootMenu
  #
  # ZBM is built + published out-of-band by `mise run zbm:build && zbm:upload`.
  # qemu.pkr.hcl reads the architecture-specific release from
  # group_vars/all/versions.yml and passes it here as $ZBM_VERSION.
  #
  # Only the unified ZBM EFI image (/EFI/ZBM/VMLINUZ.EFI) and its cmdline are
  # installed; older tarballs also carry an unused components-mode kernel +
  # initrd. rEFInd ships as refind_x64.efi on x86_64 and refind_aa64.efi on
  # aarch64 ($REFIND_NAME, derived from `uname -m` above).
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

  # Configure rEFInd

  # Run refind-install once, explicitly, below. Left at its default, the
  # package postinst runs it on this install and again on every upgrade.
  echo "refind refind/install_to_esp boolean false" | debconf-set-selections

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

  # Menu countdown, matching the role's refind_timeout (a converge overwrites
  # this file with that value): 3 on hosts, -1 (boot the default unless a key
  # is held) on qemu fixtures, where nobody watches the menu.
  REFIND_TIMEOUT=3
  if [ "$INSTALL_TARGET" = "qemu" ]; then
    REFIND_TIMEOUT=-1
  fi

  cat <<EOF >/boot/efi/EFI/refind/refind.conf
timeout $REFIND_TIMEOUT
default_selection "Ubuntu (ZBM)"
dont_scan_dirs EFI:/EFI/ZBM

# Twin of the converge-time roles/refind/templates/refind.conf.j2, kept in sync by
# hand.
menuentry "Ubuntu (ZBM)" {
    loader /EFI/ZBM/VMLINUZ.EFI
    options "$ZBM_CMDLINE $COMMANDLINE zbm.prefer=rpool!! zbm.skip"
    submenuentry "Show ZFSBootMenu" {
      options "$ZBM_CMDLINE $COMMANDLINE zbm.show"
    }
}
EOF

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
  # "rEFInd" entry.
  #
  # -p 2 is the ESP: provision.sh's layout is 1=bios(EF02), 2=efi(EF00),
  # 3=swap, 4=podman, 5=rpool, 6=meta. Keep these efibootmgr -p values in sync
  # with that order -- a stale -p (e.g. 1, the BIOS-boot partition) registers a
  # boot entry the firmware can't load.
  if [ "$disks_count" -eq 1 ]; then
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
}

install_boot_loader

# Enable tmp mount. Noble ships tmp.mount as a disabled template under
# /usr/share/systemd/; enable it by path, which links it. Resolute ships
# /usr/lib/systemd/system/tmp.mount already enabled, so there is nothing to do.

if [ -f /usr/share/systemd/tmp.mount ]; then
  systemctl enable /usr/share/systemd/tmp.mount
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

# Disable ssh password authentication. The login user created below is
# key-only (no password set); no other user can log in. Drop a snippet under
# sshd_config.d so the override wins even if /etc/ssh/sshd_config is later
# edited.
echo 'PasswordAuthentication no' >/etc/ssh/sshd_config.d/00-hardening.conf

# User setup. Every target but Hetzner gets a key-only USERNAME sudoer: the
# qemu fixtures' `vagrant`, so the test harness can SSH back in, or the
# operator on bare metal. The hetzner image must NOT bake a login user (the
# snapshot would ship a known key on the one internet-facing host); instead it
# installs cloud-init so terraform's user_data creates `ak` + injects the SSH
# key on first boot, exactly as the stock hcloud image does.
if [ "$INSTALL_TARGET" = "hetzner" ]; then
  # cloud-guest-utils ships the growpart binary cloud-init's growpart module
  # runs.
  apt-get install --yes cloud-init cloud-guest-utils

  # The Hetzner cloud-init drop-in the stock hcloud image ships, captured
  # verbatim under packer/hetzner/ (Noble and Resolute ship the same file). Its
  # mirror.hetzner.com package_mirrors and module set make this debootstrapped
  # cloud-init behave like the stock image's: apt_configure points the sources
  # at the Hetzner mirror on first boot. Its default_user is root, but
  # terraform's user-data `users:` block replaces that list with `ak`, the sole
  # login user, with root locked.
  install -m 0644 "${CHROOT_FILES}/hetzner/90-hetznercloud.cfg" /etc/cloud/cloud.cfg.d/

  # Sorted last, so it wins: pins the Hetzner datasource, so this cloud-init
  # finds the metadata and user-data without probing the full list, and points
  # growpart at rpool's partition.
  install -m 0644 "${CHROOT_FILES}/hetzner/99-hetzner.cfg" /etc/cloud/cloud.cfg.d/

  # The image ships at 60G but deploys onto cpx22's ~76G, leaving rpool's
  # partition (p5, last on disk) short with the GPT backup header mid-disk. The
  # preceding 40G Podman partition stays fixed while p5 takes the added
  # capacity: cloud-init's growpart module grows p5 on every boot (growpart
  # relocates the backup header), and zfs_expand_rpool.service then grows the
  # pool into it.
  zpool set autoexpand=on rpool
  install -m 0755 "${CHROOT_FILES}/hetzner/zfs_expand_rpool.sh" /usr/local/sbin/zfs_expand_rpool
  install -m 0644 "${CHROOT_FILES}/hetzner/zfs_expand_rpool.service" /etc/systemd/system/
  systemctl enable zfs_expand_rpool.service

else
  # The login user: vagrant on the qemu fixtures, the operator on bare metal.
  adduser --disabled-password --gecos "" "$USERNAME"

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

# Mirror the journal onto the virtio console the harness always attaches. The
# unit (test/homelab_guest_journal.service) is shared with the minimal
# fixture's cloud-init seed.
if [ "$INSTALL_TARGET" = "qemu" ]; then
  install -m 0644 "${CHROOT_FILES}/homelab_guest_journal.service" /etc/systemd/system/
  # verify exits 0 on "Unknown key name ... ignoring", so a misplaced
  # directive passes the bake and then fails every unit the systemd_unit
  # helper validates on the fixture. Treat any complaint as fatal here, as
  # well as a failing exit.
  if ! verify_output=$(systemd-analyze verify /etc/systemd/system/homelab_guest_journal.service 2>&1) ||
    [ -n "$verify_output" ]; then
    echo "$verify_output" >&2
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

# Keep background apt work from taking the dpkg lock in QEMU cells. The
# unattended_upgrades role unmasks its units; the boot role owns the multipath
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
write_sources_list "$UBUNTU_NAME" "$UBUNTU_MIRROR_UPSTREAM" "$UBUNTU_MIRROR_SECURITY_UPSTREAM"

# Refresh /var/lib/apt/lists/ under the upstream URLs (write_sources_list
# just cleared the build-time Nexus lists) so the shipped image carries a
# coherent cache: package tasks using cache_valid_time may skip their own update
# and would otherwise find no candidate.
apt_update

# Drop the downloaded .deb cache (build-only, ~hundreds of MB) so it doesn't
# ride into every deployment. Clears /var/cache/apt/archives only — the
# lists/ repopulated just above stay intact.
apt-get clean

# dbus keeps its own copy of the machine-id; point it at systemd's, which
# provision.sh blanks for a fresh image.
if [ -e /var/lib/dbus/machine-id ]; then
  ln -sf /etc/machine-id /var/lib/dbus/machine-id
fi

# Drop build-time logs (dpkg/apt/debootstrap) so the image starts clean.
# Files only — keep the dir tree services expect. Last, to catch the
# writes from the steps just above.
find /var/log -type f -delete
