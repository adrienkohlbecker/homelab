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
# --owner=0/--group=0 bake root:0 into the archive metadata so extracting
# in a chroot or onto FAT32 doesn't trip "Cannot change ownership"
# from the container's build-user uids leaking into the archive.
tarball="zfsbootmenu-v${ZBM_VERSION}-${arch}.tar.gz"
(cd "$out_dir" && tar --owner=0 --group=0 -czf "$tarball" vmlin*-bootmenu initramfs-bootmenu.img vmlinuz.EFI)
sha256sum "$out_dir/$tarball"
