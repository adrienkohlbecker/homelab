#!/usr/bin/env bash
#MISE description="Build a ZFSBootMenu recovery tarball with upstream zbm-builder.sh"
set -euo pipefail

# shellcheck source=mise-tasks/zbm/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

arch="$(zbm_host_arch)"
if [ -z "${ZBM_BUILD_SUFFIX:-}" ] && [ -z "${CI:-}" ]; then
  ZBM_BUILD_SUFFIX="-local.$(date "+%Y%m%d%H%M%S")"
fi
repo_root="$(zbm_repo_root)"
src_dir="${repo_root}/zbm-build/src"
out_dir="${repo_root}/zbm-build/${arch}"
builder_tag="localhost/zbm-builder:v${ZBM_VERSION}-${arch}"

mkdir -p "$out_dir"
rm -f "$out_dir"/*

if [ ! -d "$src_dir/.git" ]; then
  echo "ZBM source not found at $src_dir — run 'mise run zbm:builder-image' first" >&2
  exit 1
fi

workdir="$(mktemp -d "${repo_root}/zbm-build/build.${arch}.XXXXXX")"
trap 'rm -rf "$workdir"' EXIT INT TERM
build_root="${workdir}/root"
package_dir="${workdir}/package"
mkdir -p "$build_root" "$package_dir"
cp -a "${repo_root}/zbm/." "$build_root/"

# Keep build inputs under the checkout so GitLab dind can bind-mount them.
bash "$src_dir/zbm-builder.sh" \
  -d \
  -b "$build_root" \
  -i "$builder_tag" \
  -l "$src_dir" \
  -H \
  -O --entrypoint=/build-init.sh \
  -O --env=DRACUT_NO_XATTR=1 \
  -- -b /build -V

build_output="${build_root}/build"
mapfile -t kernel_images < <(find "$build_output" -maxdepth 1 -type f -name 'vmlin*-bootmenu')
if [ "${#kernel_images[@]}" -ne 1 ]; then
  echo "expected exactly one vmlin*-bootmenu in $build_output, found ${#kernel_images[@]}" >&2
  exit 1
fi
mapfile -t efi_images < <(find "$build_output" -maxdepth 1 -type f -name 'vmlin*.EFI')
if [ "${#efi_images[@]}" -ne 1 ]; then
  echo "expected exactly one vmlin*.EFI in $build_output, found ${#efi_images[@]}" >&2
  exit 1
fi
if [ ! -s "${build_output}/initramfs-bootmenu.img" ]; then
  echo "missing initramfs-bootmenu.img in $build_output" >&2
  exit 1
fi

cp "${kernel_images[0]}" "$package_dir/"
cp "${build_output}/initramfs-bootmenu.img" "$package_dir/"
cp "${efi_images[0]}" "$package_dir/zfsbootmenu.EFI"
yq -er '.Kernel.CommandLine' "${repo_root}/zbm/config.yaml" >"$package_dir/cmdline"

initramfs_listing="${workdir}/initramfs.lsinitrd"
docker run --rm \
  --entrypoint /usr/bin/lsinitrd \
  -v "${package_dir}:/work:ro" \
  "$builder_tag" \
  /work/initramfs-bootmenu.img >"$initramfs_listing"

zbm_assert_core_listing "$initramfs_listing"

if [ "$arch" = "aarch64" ] && ! grep -Eq "/efivarfs[.]ko([.]|$)" "$initramfs_listing"; then
  echo "ZBM initramfs is missing required EFI variable filesystem module: efivarfs.ko" >&2
  exit 1
fi

tarball="zfsbootmenu-v${ZBM_VERSION}-linux${ZBM_KERNEL_VERSION}${ZBM_BUILD_SUFFIX:-}-${arch}.tar.gz"
(cd "$package_dir" && tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --format=ustar -cf - vmlin*-bootmenu initramfs-bootmenu.img zfsbootmenu.EFI cmdline | gzip -n >"${out_dir}/${tarball}")
(cd "$out_dir" && sha256sum "$tarball" | tee "${tarball}.sha256sum")
