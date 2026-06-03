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

# Stage the stable recovery-SSH host private key into the build dir for
# crypt-ssh to bake in (recovery.conf points dropbear_ed25519_key here). The
# plaintext is gitignored and removed on exit; only the committed .pub rides in
# the repo. Resolved from 1Password at the point of use (op read into a local,
# never-exported var) so the key never enters the environment of the
# zbm-builder.sh / container-build subtree. Mirrors upload.sh's inline op read.
host_key="${MISE_CONFIG_ROOT}/zbm/dropbear/ssh_host_ed25519_key"
trap 'rm -f "$host_key"' EXIT INT TERM
key_material="$(op read 'op://Lab/hxad25fxm2gfulafg23b6sv33e/private key')"
(umask 077 && printf '%s\n' "$key_material" > "$host_key")

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
# "/${ZBM_BUILDER}", which crun can't find. (The old vendored Dockerfile
# hard-coded /build-init.sh, so it never hit this.) Override it here.
"$src_dir/zbm-builder.sh" \
  -b "${MISE_CONFIG_ROOT}/zbm" \
  -i "localhost/zbm-builder:v${ZBM_VERSION}-${arch}" \
  -l "$src_dir" \
  -H \
  -O --entrypoint -O /build-init.sh \
  -O -v -O "$out_dir:/output" \
  -- -o /output -t "v${ZBM_VERSION}"

# Both modes emit into $out_dir:
#   Components: vmlin{u,uz}-bootmenu + initramfs-bootmenu.img
#   EFI:        vmlinuz.EFI (unified kernel image)
# Rename the unified EFI to zfsbootmenu.EFI before archiving: on the
# case-insensitive FAT32 ESP, vmlinuz.EFI == VMLINUZ.EFI — the path the
# zfsbootmenu role deploys the stock single-file recovery UKI to — so unpacking
# our tarball there would clobber it. A distinct name lets both coexist.
if [ -f "$out_dir/vmlinuz.EFI" ]; then
  mv "$out_dir/vmlinuz.EFI" "$out_dir/zfsbootmenu.EFI"
fi

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
