#!/usr/bin/env bash
#MISE description="Build a ZFSBootMenu kernel + initrd tarball via the local builder container"
# Calls upstream's zbm-builder.sh with zbm/ as the build directory and
# the cloned source tree mounted read-only at /zbm. Components mode:
# emits vmlinuz-bootmenu + initramfs-bootmenu.img (no unified .EFI).
# We tarball both into a single artifact suitable for upload to Gitea.
set -euo pipefail

arch="$(uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/)"
src_dir="${MISE_CONFIG_ROOT}/zbm-build/src"
out_dir="${MISE_CONFIG_ROOT}/zbm-build/${arch}"
mkdir -p "$out_dir"
rm -f \
  "$out_dir"/*-bootmenu \
  "$out_dir"/*-bootmenu.img \
  "$out_dir"/zfsbootmenu-v*-"${arch}".tar.gz \
  "$out_dir"/zfsbootmenu-v*-"${arch}".tar.gz.sha256sum \
  "$out_dir"/*.EFI

if [ ! -d "$src_dir" ]; then
  echo "ZBM source not found at $src_dir — run 'mise run zbm:builder-image' first" >&2
  exit 1
fi

# Stage the recovery-SSH host private key into the build dir for crypt-ssh to
# bake in (recovery.conf points dropbear_ed25519_key here). The plaintext is
# gitignored and removed on exit; only the committed .pub rides in the repo.
host_key="${MISE_CONFIG_ROOT}/zbm/dropbear/ssh_host_ed25519_key"
trap 'rm -f "$host_key"' EXIT INT TERM
if [ "${ZBM_EPHEMERAL_HOST_KEY:-}" = "1" ]; then
  # CI validation builds have no 1Password: bake a throwaway host key so the
  # image still builds and the SSH server still starts. It won't match the
  # committed .pub, so the tarball is NOT publishable — a build-check only.
  # ssh-keygen into a tempdir so its .pub sibling can't overwrite the committed
  # dropbear/ssh_host_ed25519_key.pub.
  echo "ZBM_EPHEMERAL_HOST_KEY=1: baking a throwaway recovery host key (do not upload this build)" >&2
  ephemeral_dir="$(mktemp -d)"
  trap 'rm -f "$host_key"; rm -rf "$ephemeral_dir"' EXIT INT TERM
  ssh-keygen -q -t ed25519 -N '' -C zbm-recovery-ephemeral -f "$ephemeral_dir/key"
  (umask 077 && cp "$ephemeral_dir/key" "$host_key")
else
  # Resolved from 1Password at the point of use (op read into a local,
  # never-exported var) so the key never enters the environment of the
  # zbm-builder.sh / container-build subtree. Mirrors upload.sh's inline op read.
  if ! key_material="$(op read 'op://Lab/hxad25fxm2gfulafg23b6sv33e/private key')"; then
    echo "op read failed — is the 1Password CLI signed in? Try: op signin" >&2
    exit 1
  fi
  (umask 077 && printf '%s\n' "$key_material" >"$host_key")
fi

# zbm-builder.sh:
#   -b: build directory (zbm/ — contains config.yaml, dracut.conf.d/, hooks/)
#   -i: builder container image
#   -l: ZBM source tree (mounted read-only at /zbm)
#   -H: skip hostid (release build, not host-specific)
#   -O: extra podman run args (output volume + entrypoint override)
#   --: args forwarded to build-init.sh (-o output dir, -t version tag)
#
# zbm-builder.sh auto-detects hooks/ and generates user_hooks.conf.
#
# --entrypoint /build-init.sh: the upstream Dockerfile declares
#   ENTRYPOINT [ "/${ZBM_BUILDER}" ]
# in exec form, and BuildKit does not expand the ARG in exec-form
# ENTRYPOINT — the built image's entrypoint is the literal string
# "/${ZBM_BUILDER}", which crun can't find. Override it here.
#
# DRACUT_NO_XATTR=1: dracut-install copies install_items/hooks via
# `cp --preserve=...,xattr,...`, which hard-fails ("setting attributes:
# Operation not supported") whenever the destination can't take an xattr the
# source carries. On a Mac podman machine both triggers fire: source files on
# the virtiofs bind mount carry macOS's unremovable com.apple.provenance (an
# invalid Linux xattr namespace), and the container overlay is mounted with a
# fixed SELinux context= that rejects security.selinux. xattrs are meaningless
# in the initramfs (everything runs as root: no SELinux, no file capabilities),
# so tell dracut-install to skip them. No-op on Linux/CI where neither trigger
# exists; set unconditionally to keep local and CI builds identical.
#
# Invoke via `bash` (not the script's `#!/bin/bash` shebang) because zbm-builder.sh
# uses bash-4 lowercase expansion (${var,,}). macOS's /bin/bash is 3.2, where that
# errors with "bad substitution" — harmless today (the failed `case` falls through
# to its safe default) but fragile. PATH resolves bash to Homebrew 5.x on the Mac
# and the system bash 5.x on CI, so the script runs as upstream intended.
bash "$src_dir/zbm-builder.sh" \
  -b "${MISE_CONFIG_ROOT}/zbm" \
  -i "localhost/zbm-builder:v${ZBM_VERSION}-${arch}" \
  -l "$src_dir" \
  -H \
  -O --entrypoint -O /build-init.sh \
  -O --env -O DRACUT_NO_XATTR=1 \
  -O -v -O "$out_dir:/output" \
  -- -o /output -t "v${ZBM_VERSION}"

# Both modes emit into $out_dir:
#   Components: vmlin{u,uz}-bootmenu + initramfs-bootmenu.img
#   EFI:        the unified image, named after the kernel — vmlinuz.EFI on
#               x86_64, vmlinux.EFI on aarch64 (Void's arm64 kernel is vmlinux).
# Rename the unified EFI to zfsbootmenu.EFI before archiving: on the
# case-insensitive FAT32 ESP, vmlinuz.EFI == VMLINUZ.EFI — the path the
# zfsbootmenu role deploys the stock single-file recovery UKI to — so unpacking
# our tarball there would clobber it. A distinct name lets both coexist. Match
# either spelling so the archive member is arch-independent; assert exactly one.
mapfile -t efi_images < <(find "$out_dir" -maxdepth 1 -type f -name 'vmlin*.EFI')
if [ "${#efi_images[@]}" -ne 1 ]; then
  echo "expected exactly one vmlin*.EFI in $out_dir, found ${#efi_images[@]}: ${efi_images[*]:-none}" >&2
  exit 1
fi
mv "${efi_images[0]}" "$out_dir/zfsbootmenu.EFI"

# --sort=name + --mtime=@0 + --numeric-owner, piped through gzip -n, strip the
# build-time mtimes and uid/gid names that tar and the gzip header would
# otherwise embed, so rebuilds of a pinned ZBM_VERSION produce a byte-identical
# archive (pairs with reproducible=yes, which stabilizes the cpio inside it).
# --owner=0/--group=0 also bake root:0 into the metadata so extracting in a
# chroot or onto FAT32 doesn't trip "Cannot change ownership" from the
# container's build-user uids leaking into the archive.
tarball="zfsbootmenu-v${ZBM_VERSION}-${arch}.tar.gz"
(cd "$out_dir" && tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -cf - vmlin*-bootmenu initramfs-bootmenu.img zfsbootmenu.EFI | gzip -n >"$tarball")
sha256sum "$out_dir/$tarball"
