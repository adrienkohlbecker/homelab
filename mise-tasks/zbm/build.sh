#!/usr/bin/env bash
#MISE description="Build a ZFSBootMenu kernel + initrd tarball via the local builder container"
# Runs localhost/zbm-builder:<version>-<arch> with zbm/ bind-mounted at
# /build (config.yaml + dracut.conf.d live there, read-only) and
# zbm-build/<arch>/ at /output. Components mode: the container emits
# vmlinuz-bootmenu (or vmlinux-bootmenu on aarch64) + initramfs-bootmenu.img
# rather than a unified .EFI. We tarball both into a single artifact
# suitable for upload to Gitea releases. (The systemd-boot UKI stub
# silently fails to invoke the kernel under EDK2/aarch64 on QEMU virt;
# letting rEFInd do the handoff via loader/initrd directives works on
# both arches.)
set -eu

arch="$(uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/)"
out_dir="${MISE_CONFIG_ROOT}/zbm-build/${arch}"
mkdir -p "$out_dir"
rm -f \
  "$out_dir"/*-bootmenu \
  "$out_dir"/*-bootmenu.img \
  "$out_dir"/zfsbootmenu-v*-"${arch}".tar.gz \
  "$out_dir"/zfsbootmenu-v*-"${arch}".tar.gz.sha256sum \
  "$out_dir"/*.EFI

podman run --rm \
  -v "${MISE_CONFIG_ROOT}/zbm:/build:ro" \
  -v "$out_dir:/output" \
  "localhost/zbm-builder:v${ZBM_VERSION}-${arch}" \
  -o /output -t "v${ZBM_VERSION}"

# Components mode emits vmlin{u,uz}-bootmenu + initramfs-bootmenu.img;
# tar them together preserving upstream names so chroot.sh can glob.
# --owner=0/--group=0 bake root:0 into the archive metadata so extracting
# in a chroot or onto FAT32 doesn't trip "Cannot change ownership"
# from the container's build-user uids leaking into the archive.
tarball="zfsbootmenu-v${ZBM_VERSION}-${arch}.tar.gz"
(cd "$out_dir" && tar --owner=0 --group=0 -czf "$tarball" vmlin*-bootmenu initramfs-bootmenu.img)
sha256sum "$out_dir/$tarball"
