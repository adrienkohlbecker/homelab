#!/usr/bin/env bash
#MISE description="Build a ZFSBootMenu recovery tarball through upstream make-binary.sh"
set -euo pipefail

arch="$(uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/)"
upstream_arch="$(uname -m | sed -e s/amd64/x86_64/)"
zbm_base_version="$ZBM_VERSION"
if [ -z "${ZBM_BUILD_SUFFIX:-}" ] && [ -z "${CI:-}" ]; then
  ZBM_BUILD_SUFFIX="-local.$(date "+%Y%m%d%H%M%S")"
fi
zbm_artifact_version="${zbm_base_version}-linux${ZBM_KERNEL_VERSION}${ZBM_BUILD_SUFFIX:-}"
repo_root="${MISE_CONFIG_ROOT}"
src_dir="${repo_root}/zbm-build/src"
out_dir="${repo_root}/zbm-build/${arch}"
builder_tag="localhost/zbm-builder:v${zbm_base_version}-${arch}"

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

builder_entrypoint="$(podman image inspect "$builder_tag" --format '{{json .Config.Entrypoint}}' 2>/dev/null || true)"
if [ "$builder_entrypoint" != '["/build-init.sh"]' ]; then
  echo "ZBM builder image ${builder_tag} has entrypoint ${builder_entrypoint:-<missing>}, expected [\"/build-init.sh\"]" >&2
  echo "run 'mise run zbm:builder-image' to rebuild the upstream-compatible local builder image" >&2
  exit 1
fi
if ! podman run --rm --entrypoint /usr/bin/bash "$builder_tag" -lc 'command -v dropbear >/dev/null && test -f /usr/lib/dracut/modules.d/60crypt-ssh/module-setup.sh'; then
  echo "ZBM builder image ${builder_tag} is missing dropbear or the crypt-ssh dracut module" >&2
  echo "run 'mise run zbm:builder-image' to rebuild the recovery-capable local builder image" >&2
  exit 1
fi

workdir="$(mktemp -d "${repo_root}/zbm-build/make-binary.${arch}.XXXXXX")"
cleanup() {
  rm -rf "$workdir"
}
trap cleanup EXIT INT TERM

wrapper_dir="${workdir}/bin"
mkdir -p "$wrapper_dir"
if command -v grealpath >/dev/null 2>&1; then
  ln -s "$(command -v grealpath)" "${wrapper_dir}/realpath"
else
  cat >"${wrapper_dir}/realpath" <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail

paths=()
for arg in "$@"; do
  case "$arg" in
    -e)
      ;;
    -q)
      ;;
    --)
      shift
      paths+=( "$@" )
      break
      ;;
    -*)
      echo "realpath wrapper: unsupported option ${arg}" >&2
      exit 1
      ;;
    *)
      paths+=( "$arg" )
      ;;
  esac
done

for path in "${paths[@]}"; do
  [ -e "$path" ] || exit 1
  python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$path"
done
WRAPPER
  chmod +x "${wrapper_dir}/realpath"
fi
real_podman="$(command -v podman)"
{
  printf '#!/usr/bin/env bash\n'
  printf 'set -euo pipefail\n'
  printf 'real_podman=%q\n' "$real_podman"
  cat <<'WRAPPER'
args=()
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-v" ] && [ "$#" -ge 2 ]; then
    volume="$2"
    case "$volume" in
      .:*) volume="${PWD}${volume#.}" ;;
    esac
    args+=( "-v" "$volume" )
    shift 2
    continue
  fi
  args+=( "$1" )
  shift
done
exec "$real_podman" "${args[@]}"
WRAPPER
} >"${wrapper_dir}/podman"
chmod +x "${wrapper_dir}/podman"
export PATH="${wrapper_dir}:${PATH}"

work_src="${workdir}/src"
git clone --no-hardlinks "$src_dir" "$work_src" >/dev/null
git -c advice.detachedHead=false -C "$work_src" checkout -q "v${zbm_base_version}"
git -C "$work_src" reset --hard "v${zbm_base_version}" >/dev/null
git -C "$work_src" clean -fdx >/dev/null

python3 - "${work_src}/dracut/module-setup.sh" <<'PY'
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    contents = f.read()

needle = "  # Install core ZFSBootMenu functionality\n"
replacement = """  # The builder rootfs includes runit-void power commands. Dracut's
  # base/shutdown modules install those before 90zfsbootmenu; remove them from
  # the initramfs staging tree so ZBM's pool-exporting wrappers win.
  rm -f \\
    \"${initdir}/usr/bin/firmware-setup\" \\
    \"${initdir}/usr/bin/poweroff\" \\
    \"${initdir}/usr/bin/reboot\" \\
    \"${initdir}/usr/bin/shutdown\"

  # Install core ZFSBootMenu functionality
"""
if needle not in contents:
    raise SystemExit(f"{path}: expected ZFSBootMenu core install marker not found")

with open(path, "w", encoding="utf-8") as f:
    f.write(contents.replace(needle, replacement, 1))
PY

python3 - "${work_src}/releng/make-binary.sh" <<'PY'
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    contents = f.read()

old_case = """case "${arch}" in
  x86_64) BUILD_EFI="true" ;;
  *) BUILD_EFI="false" ;;
esac
"""
new_case = 'BUILD_EFI="true"\n'

old_copy = """      if ! cp "${outdir}/vmlinuz.EFI" "${assets}/${efifile}"; then
        error "failed to copy UEFI bundle"
      fi

      # Remove it so it won't be included in component tarballs
      rm -f "${outdir}/vmlinuz.EFI"
"""
new_copy = """      efi_images=()
      while IFS= read -r efi_image; do
        efi_images+=( "${efi_image}" )
      done < <(find "${outdir}" -maxdepth 1 -type f -name 'vmlin*.EFI' | sort)
      if [ "${#efi_images[@]}" -ne 1 ]; then
        error "expected exactly one UEFI bundle in ${outdir}, found ${#efi_images[@]}"
      fi
      if ! cp "${efi_images[0]}" "${assets}/${efifile}"; then
        error "failed to copy UEFI bundle"
      fi

      # Remove it so it won't be included in component tarballs
      rm -f "${efi_images[0]}"
"""

for needle, replacement in ((old_case, new_case), (old_copy, new_copy)):
    if needle not in contents:
        raise SystemExit(f"{path}: expected make-binary.sh snippet not found")
    contents = contents.replace(needle, replacement, 1)

with open(path, "w", encoding="utf-8") as f:
    f.write(contents)
PY

homelab_root="${work_src}/homelab"
mkdir -p "${homelab_root}/dropbear"
cp -a "${repo_root}/zbm/hooks" "${homelab_root}/"

ssh-keygen -q -t ed25519 -N '' -C zbm-recovery -f "${homelab_root}/dropbear/ssh_host_ed25519_key"
cp "${homelab_root}/dropbear/ssh_host_ed25519_key.pub" "$out_dir/ssh_host_ed25519_key.pub"
cp "${repo_root}/zbm/dropbear/authorized_keys" "${homelab_root}/dropbear/"
echo "Recovery SSH host key fingerprint:"
ssh-keygen -E sha256 -lf "$out_dir/ssh_host_ed25519_key.pub"

python3 - "${repo_root}/zbm/config.yaml" "${work_src}/etc/zfsbootmenu/recovery.yaml" <<'PY'
import sys
import yaml

overlay_path, upstream_path = sys.argv[1:]
with open(overlay_path, encoding="utf-8") as f:
    overlay = yaml.safe_load(f)
with open(upstream_path, encoding="utf-8") as f:
    upstream = yaml.safe_load(f)

upstream["Kernel"]["CommandLine"] = overlay["Kernel"]["CommandLine"]

with open(upstream_path, "w", encoding="utf-8") as f:
    yaml.safe_dump(upstream, f, sort_keys=False)
PY
cp "${repo_root}/zbm/dracut.conf.d/recovery.conf" "${work_src}/etc/zfsbootmenu/recovery.conf.d/zz-homelab-recovery.conf"
cp "${repo_root}/zbm/dracut.conf.d/user_hooks.conf" "${work_src}/etc/zfsbootmenu/recovery.conf.d/zz-homelab-user-hooks.conf"

(
  cd "$work_src"
  ./releng/make-binary.sh "$zbm_base_version" "$builder_tag"
)

asset_base="zfsbootmenu-recovery-${upstream_arch}-v${zbm_base_version}-linux${ZBM_KERNEL_VERSION}"
component_dir_name="zfsbootmenu-recovery-${upstream_arch}-v${zbm_base_version}"
asset_dir="${work_src}/releng/assets/${zbm_base_version}"
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
component_dir="${extract_dir}/${component_dir_name}"
if [ ! -d "$component_dir" ]; then
  echo "upstream tarball missing expected component directory: $component_dir_name" >&2
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

python3 -c "
import yaml
doc = yaml.safe_load(open('${work_src}/etc/zfsbootmenu/recovery.yaml'))
print(doc['Kernel']['CommandLine'])
" >"$out_dir/cmdline"

initramfs_listing="$(
  podman run --rm \
    --entrypoint /usr/bin/lsinitrd \
    -v "$out_dir:/output:ro" \
    "$builder_tag" \
    /output/initramfs-bootmenu.img
)"

for required in dropbear authorized_keys; do
  if ! grep -qF "$required" <<<"$initramfs_listing"; then
    echo "Included dracut modules:" >&2
    awk '/^dracut modules:$/ { show=1; next } show && NF == 1 { print "  " $1; next } show && NF != 1 { exit }' <<<"$initramfs_listing" >&2
    echo "ZBM initramfs is missing required recovery SSH item: ${required}" >&2
    exit 1
  fi
done

for required in \
  "usr/bin/reboot" \
  "usr/bin/poweroff -> reboot" \
  "usr/bin/shutdown -> reboot" \
  "usr/bin/firmware-setup -> reboot"; do
  if ! grep -qF "$required" <<<"$initramfs_listing"; then
    echo "Power command paths in ZBM initramfs:" >&2
    awk 'NF >= 9 && $9 ~ /(^|\/)(reboot|poweroff|shutdown|firmware-setup)$/ { print "  " $9, $10, $11 }' <<<"$initramfs_listing" >&2
    echo "ZBM initramfs is missing required ZFSBootMenu recovery command: /${required%% *}" >&2
    exit 1
  fi
done

for required in zfs spl; do
  if ! grep -Eq "/${required}[.]ko([.]|$)" <<<"$initramfs_listing"; then
    echo "ZBM initramfs is missing required ZFS kernel module: ${required}.ko" >&2
    exit 1
  fi
done

if [ "$arch" = "aarch64" ] && ! grep -Eq "/efivarfs[.]ko([.]|$)" <<<"$initramfs_listing"; then
  echo "ZBM initramfs is missing required EFI variable filesystem module: efivarfs.ko" >&2
  exit 1
fi

tarball="zfsbootmenu-v${zbm_artifact_version}-${arch}.tar.gz"
(cd "$out_dir" && tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --format=ustar -cf - vmlin*-bootmenu initramfs-bootmenu.img zfsbootmenu.EFI ssh_host_ed25519_key.pub cmdline | gzip -n >"$tarball")
(cd "$out_dir" && sha256sum "$tarball" | tee "${tarball}.sha256sum")
