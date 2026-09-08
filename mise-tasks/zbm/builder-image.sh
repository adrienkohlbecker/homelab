#!/usr/bin/env bash
#MISE description="Build the zbm-builder container image locally for the host arch"
# Clones zfsbootmenu at the pinned tag, then builds the upstream
# Dockerfile via docker buildx (BuildKit). The image always targets the
# host arch (no cross-build): run on Mac arm for an aarch64 image, on
# the Linux x86_64 dev host for an amd64 image. Tagged
# localhost/zbm-builder:<version>-<arch>. Re-run when ZBM_VERSION
# changes.
#
# Why docker buildx instead of podman build:
# - upstream's Dockerfile uses BuildKit heredocs (RUN <<-EOF) and
#   --mount=type=cache, which podman 4.9 / buildah 1.33 can't parse
# - docker buildx spawns a BuildKit container (moby/buildkit) that
#   handles these natively, connected to podman via its socket
# - this keeps the build on the upstream Dockerfile and its build script
#
# Requires: docker-ce-cli + docker-buildx-plugin, podman socket active.
# On pug: `systemctl --user start podman.socket`
# If the build fails with connection errors (stale BuildKit builder):
#   docker buildx rm default && systemctl --user restart podman.socket
#
# XBPS_REPOS points at the Void Linux Frankfurt mirror because the
# upstream-default Fastly CDN throttles to <100 kB/s from this ISP,
# making the xbps-install layer painfully slow. Frankfurt is a Tier 1
# mirror with EU-local capacity. Per Void docs, glibc x86_64 lives at
# `/current` and aarch64 (still glibc) at `/current/aarch64` — pick the
# subpath that matches the host arch.
set -euo pipefail

# shellcheck source=mise-tasks/zbm/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

arch="$(zbm_host_arch)"
case "$arch" in
x86_64) xbps_repo=https://repo-de.voidlinux.org/current ;;
aarch64) xbps_repo=https://repo-de.voidlinux.org/current/aarch64 ;;
*)
  echo "unsupported host arch: $arch" >&2
  exit 1
  ;;
esac

repo_root="$(zbm_repo_root)"

src_dir="${repo_root}/zbm-build/src"
mkdir -p "$(dirname "$src_dir")"
if [ -d "$src_dir/.git" ] &&
  [ "$(git -C "$src_dir" describe --tags --exact-match 2>/dev/null)" = "v${ZBM_VERSION}" ]; then
  echo "ZBM source at $src_dir already at v${ZBM_VERSION}, skipping clone"
else
  # Move the source tree into place only after the clone succeeds, so a failed
  # clone never becomes the cached source.
  rm -rf "${src_dir}".tmp.*
  tmp_dir="$(mktemp -d "${src_dir}.tmp.XXXXXX")"
  trap 'rm -rf "$tmp_dir"' EXIT INT TERM
  git clone --depth 1 --single-branch --branch "v${ZBM_VERSION}" https://github.com/zbm-dev/zfsbootmenu.git "$tmp_dir"
  rm -rf "$src_dir"
  mv "$tmp_dir" "$src_dir"
  trap - EXIT INT TERM
fi
git -C "$src_dir" reset --hard "v${ZBM_VERSION}" >/dev/null
git -C "$src_dir" clean -fdx >/dev/null
git -C "$src_dir" apply "$repo_root/zbm/recovery-overlay.patch"

# PACKAGES are extra Void packages layered onto upstream's base image to satisfy
# recovery.conf's install_items need mdadm + nvme-cli for operator recovery.
# Keep dhclient available for manual networking; the base ships no DHCP client.
img="localhost/zbm-builder:v${ZBM_VERSION}-${arch}"

# Registry-backed layer cache in the homelab GitLab project's container
# registry. --cache-from pulls prior layers so a build reuses the slow
# xbps-install layers even after the local buildkitd cache is pruned; cache
# import is best-effort, so a miss or an unauthenticated 401/404 is a
# non-fatal warning. --cache-to type=registry,mode=max pushes cache manifests
# for EVERY intermediate layer to the registry during the build (not just the
# final image's layers as inline would). This makes cache hits granular at
# the xbps-install layer level even across version bumps.
# Default empty so local workstation builds stay self-contained; the
# .gitlab-ci.yml zbm_build job sets ZBM_BUILDER_CACHE_REF explicitly.
: "${ZBM_BUILDER_CACHE_REF:=}"
cache_args=()
if [ -n "$ZBM_BUILDER_CACHE_REF" ]; then
  cache_args+=(--cache-from "type=registry,ref=${ZBM_BUILDER_CACHE_REF}")
  if [ -n "${CI:-}" ]; then
    # type=registry pushes cache manifests directly during the build; no separate
    # push step needed. Gated on $CI (set by GitLab CI) so local workstation
    # builds don't attempt an unauthenticated push.
    cache_args+=(--cache-to "type=registry,ref=${ZBM_BUILDER_CACHE_REF},mode=max")
  fi
fi
docker buildx build \
  --pull \
  --progress=plain \
  --build-arg "XBPS_REPOS=${xbps_repo}" \
  --build-arg "KERNELS=linux${ZBM_KERNEL_VERSION}" \
  --build-arg "PACKAGES=mdadm nvme-cli dhclient" \
  ${cache_args[@]+"${cache_args[@]}"} \
  --load \
  --tag "$img" \
  -f "$src_dir/releng/docker/Dockerfile" \
  "$src_dir/releng/docker"

docker run --rm --entrypoint /usr/bin/bash "$img" -lc '
  set -euo pipefail
  test -f /usr/share/perl5/core_perl/Pod/Usage.pm
'
