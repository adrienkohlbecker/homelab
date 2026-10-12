#!/bin/bash
# Install another Ubuntu release as a new boot environment,
# rpool/ROOT/$UBUNTU_NAME, beside the running root of a provisioned host. Run
# as root on that host, which keeps serving throughout.
#
# Nothing outside the new dataset is written: partitions, md arrays, the ESP,
# swap, the Podman store, and every other dataset and pool stay as they are, and
# the running root remains the rollback. The new root takes the running host's
# identity and storage wiring (hostid, machine-id, SSH host keys, fstab,
# mdadm.conf, netplan, zpool.cache, ZFS mount cache) and is otherwise a fresh
# chroot.sh install, which the first converge on it completes.
#
# The switch is left to the operator, printed at the end:
#   zpool set bootfs=rpool/ROOT/$UBUNTU_NAME rpool && systemctl reboot
# and rolling back is the same with the old root's name.
#
# Environment:
#   UBUNTU_NAME      release to install; must differ from the running one
#   TARGET_USERNAME  login user to create (an existing host user keeps its ids)
#   SSH_KEY_PUB      that user's authorized key
#   TARGET_HOSTNAME  defaults to the running hostname
#   INSTALL_TARGET   bare_metal (default) or qemu (the harness fixtures)
#   NEXUS_URL        optional apt proxy host, as for provision.sh
#   CARRY_PATHS      optional space-separated absolute paths copied as-is from
#                    the running root, for host state that lives outside a
#                    dataset of its own
#   SCRIPTS_DIR      directory holding this script's siblings (default: its own)
#   ROLE_FILES_DIR   the role-owned bootstrap files (default: SCRIPTS_DIR)
set -euxo pipefail

SCRIPTS_DIR="${SCRIPTS_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)}"
ROLE_FILES_DIR="${ROLE_FILES_DIR:-$SCRIPTS_DIR}"
# shellcheck source=packer/scripts/install_lib.sh
. "$SCRIPTS_DIR/install_lib.sh"

export INSTALL_TARGET="${INSTALL_TARGET:-bare_metal}"
export HOSTNAME="${TARGET_HOSTNAME:-$(hostname)}"
export USERNAME="${TARGET_USERNAME:-}"
export BOOT_ENVIRONMENT=1
export DEBIAN_FRONTEND=noninteractive

running_root=$(findmnt -no SOURCE /)
dataset="rpool/ROOT/${UBUNTU_NAME:-}"

preflight() {
  local path running_release
  # shellcheck source=/dev/null  # the running host's release file
  running_release=$(. /etc/os-release && echo "$VERSION_CODENAME")

  [ "$(id -u)" = 0 ] || fail "run as root"
  [ -n "${UBUNTU_NAME:-}" ] || fail "UBUNTU_NAME is required"
  [ "$UBUNTU_NAME" != "$running_release" ] || fail "$UBUNTU_NAME is the running release"
  [[ $INSTALL_TARGET =~ ^(bare_metal|qemu)$ ]] ||
    fail "INSTALL_TARGET must be bare_metal or qemu (got '$INSTALL_TARGET')"
  check_identity

  [[ $running_root == rpool/ROOT/* ]] || fail "the running root is $running_root, not a boot environment under rpool/ROOT"
  ! zfs list -H "$dataset" >/dev/null 2>&1 ||
    fail "$dataset already exists; destroy it (zfs destroy -r $dataset) to reinstall"
  [ "$(zpool status -x)" = "all pools are healthy" ] || fail "a pool is unhealthy; see zpool status -x"
  [ -s /etc/hostid ] || fail "/etc/hostid is missing; the pools would not import in the new root"
  [ -f /etc/zfs/zpool.cache ] || fail "/etc/zfs/zpool.cache is missing"

  for path in ${CARRY_PATHS:-}; do
    [[ $path == /* ]] || fail "CARRY_PATHS entries must be absolute (got '$path')"
    [ -e "$path" ] || fail "CARRY_PATHS entry $path does not exist"
  done

  check_role_files
  [ -f "$SCRIPTS_DIR/chroot.sh" ] || fail "chroot.sh not found in $SCRIPTS_DIR"
}

preflight
export_ubuntu_mirrors

# The installer tools come from the running host's own apt configuration,
# which this script leaves alone.
missing_tools=()
command -v debootstrap >/dev/null || missing_tools+=(debootstrap)
command -v arch-chroot >/dev/null || missing_tools+=(arch-install-scripts)
if ((${#missing_tools[@]})); then
  apt-get update
  apt-get install --yes "${missing_tools[@]}"
fi

# Mounted under /run, never /mnt: the running host mounts its service datasets
# there. mount.zfs's zfsutil option mounts the dataset despite its
# mountpoint=/; canmount=noauto keeps the running host from ever mounting it
# on its own.
root=$(mktemp -d /run/boot_environment.XXXXXX)
unmount_root() {
  if mountpoint -q "$root"; then
    umount "$root"
  fi
  rmdir "$root"
}
trap unmount_root EXIT

zfs create -o canmount=noauto -o mountpoint=/ "$dataset"
mount -t zfs -o zfsutil "$dataset" "$root"

debootstrap_release "$UBUNTU_NAME" "$root"

# Carry the running host's identity and storage wiring into the new root before
# chroot.sh installs the packages that read it: zfs-dracut embeds hostid,
# mdadm.conf, and the pool cache in the image; openssh-server keeps existing
# host keys; mdadm keeps an existing mdadm.conf.
mkdir -p "$root"/etc/{zfs,ssh,netplan,mdadm}
cp -a /etc/hostid /etc/machine-id /etc/fstab "$root/etc/"
cp -a /etc/zfs/zpool.cache "$root/etc/zfs/"
cp -a /etc/ssh/ssh_host_*key /etc/ssh/ssh_host_*key.pub "$root/etc/ssh/"
cp -a /etc/netplan/. "$root/etc/netplan/"
if [ -f /etc/mdadm/mdadm.conf ]; then
  cp -a /etc/mdadm/mdadm.conf "$root/etc/mdadm/"
fi

run_chroot_script "$root"

# chroot.sh creates the login user with the first free ids, which on a
# provisioned host are the existing user's. Anything else would leave the
# shared datasets' files owned by a different user in each root.
if host_ids=$(getent passwd "$USERNAME" | cut -d: -f3,4); then
  new_ids=$(awk -F: -v user="$USERNAME" '$1 == user { print $3 ":" $4 }' "$root/etc/passwd")
  [ "$new_ids" = "$host_ids" ] ||
    fail "$USERNAME is $new_ids in $dataset but $host_ids on the running host"
fi

for path in ${CARRY_PATHS:-}; do
  cp -a --parents "$path" "$root"
done

# zed rewrote the running host's ZFS mount cache when the dataset above was
# created, so copy it last. zfs-mount-generator reads it at boot to mount the
# shared datasets before the services that use them.
if [ -d /etc/zfs/zfs-list.cache ]; then
  cp -a /etc/zfs/zfs-list.cache "$root/etc/zfs/"
fi

set +x
cat <<EOF

$dataset is installed beside $running_root. To boot it:
  zpool set bootfs=$dataset rpool && systemctl reboot
To roll back afterwards:
  zpool set bootfs=$running_root rpool && systemctl reboot
EOF
