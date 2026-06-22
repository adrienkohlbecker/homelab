#!/usr/bin/env bash
#MISE description="Build a ZFSBootMenu recovery tarball through upstream make-binary.sh"
set -euo pipefail

# shellcheck source=mise-tasks/zbm/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

arch="$(zbm_host_arch)"
upstream_arch="$(zbm_upstream_arch)"
if [ -z "${ZBM_BUILD_SUFFIX:-}" ] && [ -z "${CI:-}" ]; then
  ZBM_BUILD_SUFFIX="-local.$(date "+%Y%m%d%H%M%S")"
fi
repo_root="$(zbm_repo_root)"
src_dir="${repo_root}/zbm-build/src"
out_dir="${repo_root}/zbm-build/${arch}"
builder_tag="localhost/zbm-builder:v${ZBM_VERSION}-${arch}"

mkdir -p "$out_dir"
rm -f \
  "$out_dir"/*-bootmenu \
  "$out_dir"/*-bootmenu.img \
  "$out_dir"/zfsbootmenu-v*-"${arch}".tar.gz \
  "$out_dir"/zfsbootmenu-v*-"${arch}".tar.gz.sha256sum \
  "$out_dir"/*.EFI

if [ ! -d "$src_dir/.git" ]; then
  echo "ZBM source not found at $src_dir — run 'mise run zbm:builder-image' first" >&2
  exit 1
fi

builder_entrypoint="$(docker image inspect "$builder_tag" --format '{{json .Config.Entrypoint}}' 2>/dev/null || true)"
if [ "$builder_entrypoint" != '["/build-init.sh"]' ]; then
  echo "ZBM builder image ${builder_tag} has entrypoint ${builder_entrypoint:-<missing>}, expected [\"/build-init.sh\"]" >&2
  echo "run 'mise run zbm:builder-image' to rebuild the upstream-compatible local builder image" >&2
  exit 1
fi
if ! docker run --rm --entrypoint /usr/bin/bash "$builder_tag" -lc 'command -v dropbear >/dev/null && test -f /usr/lib/dracut/modules.d/60crypt-ssh/module-setup.sh'; then
  echo "ZBM builder image ${builder_tag} is missing dropbear or the crypt-ssh dracut module" >&2
  echo "run 'mise run zbm:builder-image' to rebuild the recovery-capable local builder image" >&2
  exit 1
fi

workdir="$(mktemp -d "${repo_root}/zbm-build/make-binary.${arch}.XXXXXX")"
trap 'rm -rf "$workdir"' EXIT INT TERM

wrapper_dir="${workdir}/bin"
zbm_install_make_binary_wrappers "$wrapper_dir"
export PATH="${wrapper_dir}:${PATH}"

work_src="${workdir}/src"
git clone --no-hardlinks "$src_dir" "$work_src" >/dev/null
git -c advice.detachedHead=false -C "$work_src" checkout -q "v${ZBM_VERSION}"
git -C "$work_src" reset --hard "v${ZBM_VERSION}" >/dev/null
git -C "$work_src" clean -fdx >/dev/null

git -C "$work_src" apply "$repo_root/zbm/recovery-overlay.patch"

homelab_root="${work_src}/homelab"
mkdir -p "${homelab_root}/dropbear"
cp -a "${repo_root}/zbm/hooks" "${homelab_root}/"
cp "${repo_root}/zbm/dropbear/authorized_keys" "${homelab_root}/dropbear/"
cp "${repo_root}/zbm/dracut.conf.d/recovery.conf" "${work_src}/etc/zfsbootmenu/recovery.conf.d/zz-homelab-recovery.conf"
cp "${repo_root}/zbm/dracut.conf.d/user_hooks.conf" "${work_src}/etc/zfsbootmenu/recovery.conf.d/zz-homelab-user-hooks.conf"

ssh-keygen -q -t ed25519 -N '' -C zbm-recovery -f "${homelab_root}/dropbear/ssh_host_ed25519_key"
cp "${homelab_root}/dropbear/ssh_host_ed25519_key.pub" "$out_dir/ssh_host_ed25519_key.pub"
echo "Recovery SSH host key fingerprint:"
ssh-keygen -E sha256 -lf "$out_dir/ssh_host_ed25519_key.pub"

# Upstream stages its config and output trees under a bare mktemp -d and
# bind-mounts them into the build container. Bind-mount sources resolve on
# the docker daemon's filesystem, and under GitLab dind only the job
# checkout is shared with the daemon — a /tmp source reads back empty. Keep
# the temp tree inside the workdir so both sides see the same files.
mkdir -p "${workdir}/tmp"
(
  cd "$work_src"
  TMPDIR="${workdir}/tmp" ./releng/make-binary.sh "$ZBM_VERSION" "$builder_tag"
)

asset_base="zfsbootmenu-recovery-${upstream_arch}-v${ZBM_VERSION}-linux${ZBM_KERNEL_VERSION}"
asset_dir="${work_src}/releng/assets/${ZBM_VERSION}"
upstream_tar="${asset_dir}/${asset_base}.tar.gz"
upstream_efi="${asset_dir}/${asset_base}.EFI"
extract_dir="${workdir}/extract"

if [ ! -s "$upstream_tar" ]; then
  echo "upstream make-binary.sh did not produce expected tarball: $upstream_tar" >&2
  exit 1
fi
if [ ! -s "$upstream_efi" ]; then
  echo "upstream make-binary.sh did not produce expected EFI image: $upstream_efi" >&2
  echo "upstream make-binary.sh only enables EFI output on x86_64; run this task on the lab-class architecture" >&2
  exit 1
fi

mkdir -p "$extract_dir"
tar -xzf "$upstream_tar" -C "$extract_dir"
component_dir="${extract_dir}/zfsbootmenu-recovery-${upstream_arch}-v${ZBM_VERSION}"
if [ ! -d "$component_dir" ]; then
  echo "upstream tarball missing expected component directory: ${component_dir##*/}" >&2
  exit 1
fi

mapfile -t kernel_images < <(find "$component_dir" -maxdepth 1 -type f -name 'vmlin*-bootmenu')
if [ "${#kernel_images[@]}" -ne 1 ]; then
  echo "expected exactly one vmlin*-bootmenu in $component_dir, found ${#kernel_images[@]}" >&2
  exit 1
fi

cp "${kernel_images[0]}" "$out_dir/"
cp "${component_dir}/initramfs-bootmenu.img" "$out_dir/initramfs-bootmenu.img"
cp "$upstream_efi" "$out_dir/zfsbootmenu.EFI"

yq -er '.Kernel.CommandLine' "${work_src}/etc/zfsbootmenu/recovery.yaml" >"$out_dir/cmdline"

initramfs_listing="${workdir}/initramfs.lsinitrd"
zbm_lsinitrd "$builder_tag" "$out_dir/initramfs-bootmenu.img" >"$initramfs_listing"

for required in dropbear authorized_keys; do
  if ! grep -qF "$required" "$initramfs_listing"; then
    echo "Included dracut modules:" >&2
    awk '/^dracut modules:$/ { show=1; next } show && NF == 1 { print "  " $1; next } show && NF != 1 { exit }' "$initramfs_listing" >&2
    echo "ZBM initramfs is missing required recovery SSH item: ${required}" >&2
    exit 1
  fi
done

zbm_assert_core_listing "$initramfs_listing" "ZBM initramfs"

if [ "$arch" = "aarch64" ] && ! grep -Eq "/efivarfs[.]ko([.]|$)" "$initramfs_listing"; then
  echo "ZBM initramfs is missing required EFI variable filesystem module: efivarfs.ko" >&2
  exit 1
fi

tarball="zfsbootmenu-v${ZBM_VERSION}-linux${ZBM_KERNEL_VERSION}${ZBM_BUILD_SUFFIX:-}-${arch}.tar.gz"
(cd "$out_dir" && tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --format=ustar -cf - vmlin*-bootmenu initramfs-bootmenu.img zfsbootmenu.EFI ssh_host_ed25519_key.pub cmdline | gzip -n >"$tarball")
(cd "$out_dir" && sha256sum "$tarball" | tee "${tarball}.sha256sum")
