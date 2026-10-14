# shellcheck shell=bash
# Helpers shared by provision.sh and the chroot.sh it runs. Sourced, never
# executed.

# The installer scripts and the role-owned bootstrap files sit together
# wherever they were copied: packer uploads them to /home/vagrant, and a host
# install stages them in one directory.
SCRIPTS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

# Role-owned bootstrap files chroot.sh consumes, staged beside the scripts.
ROLE_FILES=(console-setup keyboard modules_most dracut_host.conf)

fail() {
  echo "${0##*/}: $*" >&2
  exit 1
}

# apt-get update exits 0 even when one component's Packages index fails to
# download (Nexus restart, dropped packet), leaving a partial cache that makes a
# later install fail with a baffling "Unable to locate package". --error-on=any
# turns a failed fetch into a non-zero exit. apt retries each file itself; the
# loop absorbs a mirror outage that outlasts those retries. Shared with
# chroot.sh like write_sources_list.
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

# Write deb822 apt sources for release $1 from archive mirror $2 and security
# mirror $3, matching both the stock layout and what roles/apt converges to.
# chroot.sh sources this library too.
write_sources_list() {
  # Twin of the sources roles/apt/tasks/configure.yml writes, byte for byte
  # apart from the mirror URLs, so a first converge changes only the mirror.
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

  # apt keys /var/lib/apt/lists/ by mirror URL, so changing the mirror
  # orphans the cached indices. The frozen base suite's InRelease is
  # byte-identical whichever mirror serves it (Nexus just proxies upstream),
  # so the next apt-get update records a content "Hit", skips the
  # re-download, then can't open the list file that was never written under
  # the new URL ("can not open …InRelease"). A cloud image's primed indices
  # trip the same way. Drop the cache so each rewrite re-fetches cleanly.
  find /var/lib/apt/lists -type f -delete
}

# Export the Ubuntu mirrors for this architecture. The install pulls through
# Nexus when NEXUS_URL names its host (empty or unset means upstream, as with
# group_vars' nexus_url); the installed system always ships the upstream pair.
export_ubuntu_mirrors() {
  local nexus_repositories
  case $(uname -m) in
  x86_64)
    UBUNTU_MIRROR_UPSTREAM=http://archive.ubuntu.com/ubuntu
    UBUNTU_MIRROR_SECURITY_UPSTREAM=http://security.ubuntu.com/ubuntu
    nexus_repositories=(ubuntu-archive ubuntu-security)
    ;;
  aarch64)
    UBUNTU_MIRROR_UPSTREAM=http://ports.ubuntu.com/ubuntu-ports
    UBUNTU_MIRROR_SECURITY_UPSTREAM=$UBUNTU_MIRROR_UPSTREAM
    nexus_repositories=(ubuntu-ports ubuntu-ports)
    ;;
  *) fail "unsupported architecture $(uname -m)" ;;
  esac
  if [ -n "${NEXUS_URL:-}" ]; then
    UBUNTU_MIRROR=http://$NEXUS_URL/repository/${nexus_repositories[0]}
    UBUNTU_MIRROR_SECURITY=http://$NEXUS_URL/repository/${nexus_repositories[1]}
  else
    UBUNTU_MIRROR=$UBUNTU_MIRROR_UPSTREAM
    UBUNTU_MIRROR_SECURITY=$UBUNTU_MIRROR_SECURITY_UPSTREAM
  fi
  export UBUNTU_MIRROR UBUNTU_MIRROR_SECURITY UBUNTU_MIRROR_UPSTREAM UBUNTU_MIRROR_SECURITY_UPSTREAM
}

# Check what chroot.sh gives the new root: the UBUNTU_NAME release, HOSTNAME,
# the login user USERNAME, and that user's SSH_KEY_PUB, one public key per line.
# The login user has no password and sshd refuses passwords, so every key must
# parse. The Hetzner image creates no login user; cloud-init does on first
# boot.
check_identity() {
  local key
  [[ ${UBUNTU_NAME:-} =~ ^[a-z]+$ ]] || fail "UBUNTU_NAME must be a release codename (got '${UBUNTU_NAME:-}')"
  [[ $HOSTNAME =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || fail "invalid TARGET_HOSTNAME '$HOSTNAME'"
  [[ $USERNAME =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail "invalid TARGET_USERNAME '$USERNAME'"
  if [ "$INSTALL_TARGET" = hetzner ]; then
    return 0
  fi
  [ -n "${SSH_KEY_PUB:-}" ] || fail "SSH_KEY_PUB is required"
  while IFS= read -r key; do
    if [ -n "$key" ]; then
      ssh-keygen -l -f /dev/stdin <<<"$key" >/dev/null || fail "SSH_KEY_PUB holds an unusable key: $key"
    fi
  done <<<"$SSH_KEY_PUB"
}

# Print the kernel command line ZBM boots a fresh install's roots with: the
# org.zfsbootmenu:commandline property on rpool/ROOT and the rEFInd menu's
# options. Every target keeps VGA output. QEMU fixtures add SERIAL_CONSOLE, the
# serial console verify-boot reads, and disable mitigations in disposable
# nested-KVM cells; Hetzner lists its SERIAL_CONSOLE before tty0, so the VGA
# console keeps /dev/console. Bare-metal lab and pug have no UART. The boot
# role owns the final prod command line.
boot_commandline() {
  case $INSTALL_TARGET in
  qemu) echo "console=tty0 $SERIAL_CONSOLE mitigations=off" ;;
  hetzner) echo "$SERIAL_CONSOLE console=tty0" ;;
  *) echo "console=tty0" ;;
  esac
}

# Check that every file run_chroot_script stages is here before anything is
# written.
check_install_files() {
  local file
  for file in chroot.sh "${ROLE_FILES[@]}"; do
    [ -f "$SCRIPTS_DIR/$file" ] || fail "required install file not found: $SCRIPTS_DIR/$file"
  done
  case $INSTALL_TARGET in
  qemu) [ -f "$SCRIPTS_DIR/homelab_guest_journal.service" ] || fail "homelab_guest_journal.service not found in $SCRIPTS_DIR" ;;
  hetzner) [ -d "$SCRIPTS_DIR/hetzner" ] || fail "hetzner/ not found in $SCRIPTS_DIR" ;;
  esac
}

# debootstrap release $1 into root $2 from $UBUNTU_MIRROR. If a fetch fails,
# --verbose surfaces each retrieve/validate step live and the handler dumps
# debootstrap's own log -- which otherwise dies with the build VM -- so the
# next occurrence stays diagnosable. A debootstrap older than the release
# lacks its script; every Ubuntu release since Gutsy shares that one.
debootstrap_release() {
  local release=$1 root=$2 rc
  local script=()
  if [ ! -e "/usr/share/debootstrap/scripts/$release" ]; then
    script=(/usr/share/debootstrap/scripts/gutsy)
  fi
  debootstrap --verbose "$release" "$root" "$UBUNTU_MIRROR" "${script[@]}" || {
    rc=$?
    echo "=== debootstrap failed (exit $rc); $root/debootstrap/debootstrap.log tail ===" >&2
    tail -n 300 "$root/debootstrap/debootstrap.log" >&2 || echo "(no debootstrap.log present)" >&2
    exit "$rc"
  }
}

# Mount the API filesystems chroot.sh needs inside root $1. Called in the
# private mount namespace run_chroot_script opens, so every mount here, and any
# chroot.sh adds (notably /boot/efi), disappears with it.
#
# Nothing is shared writably with the caller beyond /dev: /run and /tmp are
# private tmpfs, so a package script that regenerates runtime state (Netplan's
# generator, tmpfiles, udev rules) writes into the chroot's copy, never the
# running host's. /sys is read-only. efivarfs comes in for the boot loader
# step, which registers firmware entries. The caller's resolv.conf is bound read-only
# where the root's own resolv.conf points, so apt resolves names without the
# caller's DNS settings landing in the image.
mount_chroot_filesystems() {
  local root=$1 resolv
  mount -t proc -o nosuid,noexec,nodev proc "$root/proc"
  mount -t sysfs -o nosuid,noexec,nodev,ro sysfs "$root/sys"
  if [ -d "$root/sys/firmware/efi/efivars" ]; then
    mount -t efivarfs -o nosuid,noexec,nodev efivarfs "$root/sys/firmware/efi/efivars"
  fi
  mount -t devtmpfs -o mode=0755,nosuid udev "$root/dev"
  mount -t devpts -o mode=0620,gid=5,nosuid,noexec devpts "$root/dev/pts"
  mount -t tmpfs -o mode=1777,nosuid,nodev shm "$root/dev/shm"
  mount -t tmpfs -o mode=0755,nosuid,nodev run "$root/run"
  mount -t tmpfs -o mode=1777,nosuid,nodev tmp "$root/tmp"

  resolv=$(chroot "$root" readlink -m /etc/resolv.conf)
  if [ ! -f "$root$resolv" ]; then
    install -Dm 0644 /dev/null "$root$resolv"
  fi
  mount --bind /etc/resolv.conf "$root$resolv"
  mount -o remount,bind,ro "$root$resolv"
}

# Run chroot.sh inside root $1. Everything it reads from the installer -- this
# library, the role-owned bootstrap files, and the target's extras -- is staged
# first in one directory inside the root, passed to it as CHROOT_FILES, and
# removed afterwards. Under /var/tmp: the chroot's /tmp is a private tmpfs.
#
# `unshare --mount --propagation private` opens a throw-away mount namespace
# for the chroot's filesystems (mount_chroot_filesystems), destroyed when
# chroot.sh exits. The exported install variables flow straight through;
# list-shaped ones ride as space-delimited strings (bash refuses to export
# arrays).
run_chroot_script() {
  local root=$1 role_file
  local dir=/var/tmp/homelab_install

  rm -rf -- "${root:?}$dir"
  mkdir -p "$root$dir"
  cp "$SCRIPTS_DIR/install_lib.sh" "$SCRIPTS_DIR/chroot.sh" "$root$dir/"
  for role_file in "${ROLE_FILES[@]}"; do
    cp "$SCRIPTS_DIR/$role_file" "$root$dir/"
  done
  case $INSTALL_TARGET in
  qemu) cp "$SCRIPTS_DIR/homelab_guest_journal.service" "$root$dir/" ;;
  hetzner) cp -a "$SCRIPTS_DIR/hetzner" "$root$dir/" ;;
  esac

  # dpkg fsyncs every unpacked file by default, which dominates the install's
  # wall-clock on network-backed disks (EBS on the AWS bake) and still costs
  # plenty locally. A disposable image or fixture that crashes mid-install is
  # rebuilt, never booted, so per-file durability buys nothing there. A real
  # host keeps it: an interrupted install is costlier to redo, and the
  # local-SSD speed win is small.
  if [ "$INSTALL_TARGET" != bare_metal ]; then
    echo force-unsafe-io >"$root/etc/dpkg/dpkg.cfg.d/90-build-unsafe-io"
  fi

  # shellcheck disable=SC2016  # expanded by the inner shell
  CHROOT_FILES=$dir unshare --mount --propagation private bash -euo pipefail -c \
    "$(declare -f mount_chroot_filesystems)"'; mount_chroot_filesystems "$1"; exec chroot "$1" bash "$2" </dev/null' \
    _ "$root" "$dir/chroot.sh"

  rm -f "$root/etc/dpkg/dpkg.cfg.d/90-build-unsafe-io"
  rm -rf -- "${root:?}$dir"
}
