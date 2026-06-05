#!/usr/bin/env bash
#MISE description="Build a ZFSBootMenu kernel + initrd tarball via the local builder container"
# Calls upstream's zbm-builder.sh with zbm/ as the build directory and
# the cloned source tree mounted read-only at /zbm. Components mode:
# emits vmlinuz-bootmenu + initramfs-bootmenu.img (no unified .EFI).
# We tarball both + the unified EFI into a single artifact.
set -euo pipefail

arch="$(uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/)"
zbm_base_version="$ZBM_VERSION"
# ZBM_BUILD_SUFFIX (e.g. "-ci.12345") is appended to the version tag embedded
# in the initramfs and the tarball filename, but NOT the builder image tag.
ZBM_VERSION="${zbm_base_version}${ZBM_BUILD_SUFFIX:-}"
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

# Generate a fresh ed25519 host key for the recovery SSH server. Each build
# gets its own key — there is no stable host key to pin.
# The private key stays inside an ephemeral tmpdir for its entire lifetime and
# is never written into the repo working tree (MISE_CONFIG_ROOT / GITHUB_WORKSPACE).
# It reaches the builder container via a shadow bind-mount over /build/dropbear
# (see below), where recovery.conf's dropbear_ed25519_key=/build/dropbear/... picks
# it up. The pub key goes into the output dir for the tarball and fingerprint log.
ephemeral_dir="$(mktemp -d)"
trap 'rm -rf "$ephemeral_dir"' EXIT INT TERM
ssh-keygen -q -t ed25519 -N '' -C zbm-recovery -f "$ephemeral_dir/key"
cp "$ephemeral_dir/key.pub" "$out_dir/ssh_host_ed25519_key.pub"
echo "Recovery SSH host key fingerprint:"
ssh-keygen -E sha256 -lf "$ephemeral_dir/key.pub"

# Shadow-mount for the host key: zbm-builder.sh mounts zbm/ → /build:ro. We
# inject a second volume that shadows just /build/dropbear so the private key
# is visible inside the container (at the path recovery.conf declares) without
# landing anywhere in the repo working tree. authorized_keys lives in the repo
# and must be copied in so the shadow mount doesn't hide it.
ephemeral_dropbear="$ephemeral_dir/dropbear"
mkdir -p "$ephemeral_dropbear"
(umask 077 && cp "$ephemeral_dir/key" "$ephemeral_dropbear/ssh_host_ed25519_key")
cp "${MISE_CONFIG_ROOT}/zbm/dropbear/authorized_keys" "$ephemeral_dropbear/"

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
  -i "localhost/zbm-builder:v${zbm_base_version}-${arch}" \
  -l "$src_dir" \
  -H \
  -O --entrypoint -O /build-init.sh \
  -O --env -O DRACUT_NO_XATTR=1 \
  -O -v -O "$out_dir:/output" \
  -O -v -O "$ephemeral_dropbear:/build/dropbear:ro" \
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

# Extract the kernel version string embedded in the binary. `file` parses the
# bzImage header on x86_64; `strings` fallback covers aarch64 vmlinux (ELF,
# no bzImage header) where `file` doesn't emit a version line.
vmlinuz_path="$(ls "$out_dir"/vmlin*-bootmenu)"
kernel_ver="$(file "$vmlinuz_path" | sed -n 's/.*version \([^ ,(]*\).*/\1/p')"
if [ -z "$kernel_ver" ]; then
  kernel_ver="$(strings -n 15 "$vmlinuz_path" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+-' | head -1)"
fi
[ -n "$kernel_ver" ] || { echo "could not determine kernel version from $(basename "$vmlinuz_path")" >&2; exit 1; }
echo "Kernel version: ${kernel_ver}"

# --sort=name + --mtime=@0 + --numeric-owner, piped through gzip -n, strip the
# build-time mtimes and uid/gid names that tar and the gzip header would
# otherwise embed, so rebuilds of a pinned ZBM_VERSION produce a byte-identical
# archive (pairs with reproducible=yes, which stabilizes the cpio inside it).
# --owner=0/--group=0 also bake root:0 into the metadata so extracting in a
# chroot or onto FAT32 doesn't trip "Cannot change ownership" from the
# container's build-user uids leaking into the archive.
tarball="zfsbootmenu-v${ZBM_VERSION}-k${kernel_ver}-${arch}.tar.gz"
(cd "$out_dir" && tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --format=ustar -cf - vmlin*-bootmenu initramfs-bootmenu.img zfsbootmenu.EFI ssh_host_ed25519_key.pub | gzip -n >"$tarball")
(cd "$out_dir" && sha256sum "$tarball" | tee "${tarball}.sha256sum")
