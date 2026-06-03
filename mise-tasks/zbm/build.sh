#!/usr/bin/env bash
#MISE description="Build a ZFSBootMenu kernel + initrd tarball via the local builder container"
# Calls upstream's zbm-builder.sh with zbm/ as the build directory and
# the cloned source tree mounted read-only at /zbm. Components mode:
# emits vmlinuz-bootmenu + initramfs-bootmenu.img (no unified .EFI).
# We tarball both into a single artifact suitable for upload to Gitea.
set -euo pipefail

# Re-exec under `op run --` so ZBM_DROPBEAR_HOST_KEY's op:// reference (mise
# [env]) resolves to the real recovery-SSH host private key. File-based mise
# tasks don't get op:// auto-resolution — only toml `run` strings do.
if [[ "${ZBM_DROPBEAR_HOST_KEY:-}" == op://* && -z "${_ZBM_OP_RESOLVED:-}" ]]; then
  exec op run -- env _ZBM_OP_RESOLVED=1 "$0" "$@"
fi

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
# plaintext is gitignored and removed on exit; only the committed .pub rides
# in the repo. The op:// reference was resolved by the op-run re-exec above.
host_key="${MISE_CONFIG_ROOT}/zbm/dropbear/ssh_host_ed25519_key"
trap 'rm -f "$host_key"' EXIT
if [ -z "${ZBM_DROPBEAR_HOST_KEY:-}" ]; then
  echo "ZBM_DROPBEAR_HOST_KEY is unset (op:// ref in mise.toml [env]) — cannot stage the recovery host key" >&2
  exit 1
fi
(umask 077 && printf '%s\n' "$ZBM_DROPBEAR_HOST_KEY" > "$host_key")

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
(cd "$out_dir" && tar --sort=name --owner=0 --group=0 -czf "$tarball" vmlin*-bootmenu initramfs-bootmenu.img vmlin*.EFI)
sha256sum "$out_dir/$tarball"
