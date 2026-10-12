# shellcheck shell=bash
# Helpers shared by the installers that run chroot.sh: provision.sh (a fresh
# disk layout) and install_boot_environment.sh (a new root beside a running
# one). Sourced, never executed.

# Role-owned bootstrap files chroot.sh consumes, staged from ROLE_FILES_DIR.
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

check_role_files() {
  local role_file
  for role_file in "${ROLE_FILES[@]}"; do
    [ -f "$ROLE_FILES_DIR/$role_file" ] || fail "required role file not found: $ROLE_FILES_DIR/$role_file"
  done
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

# Run chroot.sh inside root $1. Everything it reads from the installer -- this
# library, the role-owned bootstrap files, and the target's extras -- is staged
# first in one directory inside the root, passed to it as CHROOT_FILES, and
# removed afterwards. Under /var/tmp, not /tmp: arch-chroot shadows the
# chroot's /tmp with a private tmpfs, hiding files pre-staged there.
#
# arch-chroot (arch-install-scripts) bind-mounts proc/sys/dev/devpts/run/
# efivarfs and /etc/resolv.conf for the chroot's lifetime, so apt can resolve
# hostnames without leaving the caller's DNS pinned in the target. It mounts
# into the caller's namespace, though, so any mount the chroot script adds
# (notably /boot/efi) would leak into the host and block a later unmount of the
# root. `unshare --mount --propagation private` confines everything mounted
# from here on to a throw-away namespace destroyed when unshare returns.
#
# arch-chroot inherits the calling shell's environment, so the exported
# install variables flow straight through. List-shaped ones ride as
# space-delimited strings (bash refuses to export arrays).
run_chroot_script() {
  local root=$1 role_file
  local dir=/var/tmp/homelab_install

  rm -rf -- "${root:?}$dir"
  mkdir -p "$root$dir"
  cp "$SCRIPTS_DIR/install_lib.sh" "$SCRIPTS_DIR/chroot.sh" "$root$dir/"
  for role_file in "${ROLE_FILES[@]}"; do
    cp "$ROLE_FILES_DIR/$role_file" "$root$dir/"
  done
  case $INSTALL_TARGET in
  qemu) cp "$SCRIPTS_DIR/homelab_guest_journal.service" "$root$dir/" ;;
  hetzner) cp -a "$SCRIPTS_DIR/hetzner" "$root$dir/" ;;
  esac

  CHROOT_FILES=$dir unshare --mount --propagation private arch-chroot "$root" bash "$dir/chroot.sh"

  rm -rf -- "${root:?}$dir"
}
