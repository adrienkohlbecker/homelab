#!/usr/bin/env bash
#MISE description="Build the zbm-builder container image locally for the host arch"
# Clones zfsbootmenu at the pinned tag, then builds zbm/Dockerfile
# (a podman-compatible vendored copy of upstream's
# releng/docker/Dockerfile) against the upstream releng/docker context
# so build-init.sh remains COPYable. The image always targets the host
# arch (no cross-build): run on Mac arm for an aarch64 image, on the
# Linux x86_64 dev host for an amd64 image. Tagged
# localhost/zbm-builder:<version>-<arch>. Re-run when ZBM_VERSION
# changes.
#
# Why a vendored Dockerfile:
# - upstream uses BuildKit heredocs: podman 4.9 / buildah 1.33 can't parse them
# - upstream's image-build.sh uses buildah run: rootless seccomp blocks
#   xattr extraction (libkmod.so.2, mdadm supervise dir)
# - podman build --security-opt seccomp=unconfined is the only working path
#
# --security-opt seccomp=unconfined is needed because xbps-install
# sets file capabilities/xattrs on extracted files (e.g. libelf.so.1)
# via syscalls that the default rootless seccomp profile blocks,
# yielding "Operation not permitted" on unpack. The relaxation
# applies to the build process only; the resulting image runs with
# the default profile.
#
# XBPS_REPOS points at the Void Linux Frankfurt mirror because the
# upstream-default Fastly CDN throttles to <100 kB/s from this ISP,
# making the xbps-install layer painfully slow. Frankfurt is a Tier 1
# mirror with EU-local capacity. Per Void docs, glibc x86_64 lives at
# `/current` and aarch64 (still glibc) at `/current/aarch64` — pick the
# subpath that matches the host arch.
set -euo pipefail

arch="$(uname -m | sed -e s/arm64/aarch64/ -e s/amd64/x86_64/)"
case "$arch" in
x86_64) xbps_repo=https://repo-de.voidlinux.org/current ;;
aarch64) xbps_repo=https://repo-de.voidlinux.org/current/aarch64 ;;
*)
  echo "unsupported host arch: $arch" >&2
  exit 1
  ;;
esac

src_dir="${MISE_CONFIG_ROOT}/zbm-build/src"
rm -rf "$src_dir"
mkdir -p "$(dirname "$src_dir")"
git clone --depth 1 --branch "v${ZBM_VERSION}" https://github.com/zbm-dev/zfsbootmenu.git "$src_dir"
podman build \
  --security-opt seccomp=unconfined \
  --build-arg "XBPS_REPOS=${xbps_repo}" \
  --build-arg "PACKAGES=mdadm" \
  --tag "localhost/zbm-builder:v${ZBM_VERSION}-${arch}" \
  -f "${MISE_CONFIG_ROOT}/zbm/Dockerfile" \
  "$src_dir/releng/docker"
