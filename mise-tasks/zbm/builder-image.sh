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
# - this lets us build the upstream Dockerfile unmodified
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
  # Prune any temp left by a previously-crashed clone, then clone into a fresh
  # PID-suffixed temp and move it into place only after the clone fully succeeds,
  # so a half-finished clone never becomes src (a crash mid-clone leaves only the
  # temp, pruned on the next run).
  rm -rf "${src_dir}".tmp.*
  tmp_dir="${src_dir}.tmp.$$"
  trap 'rm -rf "$tmp_dir"' EXIT INT TERM
  git clone --depth 1 --single-branch --branch "v${ZBM_VERSION}" https://github.com/zbm-dev/zfsbootmenu.git "$tmp_dir"
  rm -rf "$src_dir"
  mv "$tmp_dir" "$src_dir"
  trap - EXIT INT TERM
fi
git -C "$src_dir" reset --hard "v${ZBM_VERSION}" >/dev/null
git -C "$src_dir" clean -fdx >/dev/null

# PACKAGES are extra Void packages layered onto upstream's base image to satisfy
# recovery.conf's install_items: mdadm + nvme-cli (disk tooling), dracut-crypt-ssh
# + dropbear (recovery SSH), and dhclient for ip=single-dhcp (the base ships no
# DHCP client).
img="localhost/zbm-builder:v${ZBM_VERSION}-${arch}"

# Registry-backed layer cache in the homelab GitLab project's container
# registry. --cache-from pulls prior layers so a build reuses the slow
# xbps-install layers even after the local buildkitd cache is pruned; cache
# import is best-effort, so a miss or an unauthenticated 401/404 is a
# non-fatal warning. --cache-to type=registry,mode=max pushes cache manifests
# for EVERY intermediate layer to the registry during the build (not just the
# final image's layers as inline would). This makes cache hits granular at
# the xbps-install layer level even across version bumps, and removes the
# ordering constraint between the push and the perlfix docker commit below
# (inline cache was stripped by the commit, so inline had to push first).
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
  --build-arg "PACKAGES=mdadm nvme-cli dracut-crypt-ssh dropbear dhclient" \
  ${cache_args[@]+"${cache_args[@]}"} \
  --load \
  --tag "$img" \
  -f "$src_dir/releng/docker/Dockerfile" \
  "$src_dir/releng/docker"

# Work around a rootless-BuildKit unpack quirk: the build drops perl's
# /usr/share/perl5/core_perl/Pod/Usage.pm during xbps extraction (perl ships the
# file, but it lands missing from the built image while perl's other modules are
# fine), so generate-zbm dies at "use Pod::Usage" with "Can't locate Pod/Usage.pm".
# perl-Pod-Usage is only a virtual provide of perl, so it can't be pulled as a
# package -- but re-extracting perl via a plain container run (which unpacks
# it correctly) restores the file. The quirk only ever reproduced under a rootless buildkitd
# (the retired GitHub lab-runner path); the CI dind builder and Mac
# podman-machine builds unpack perl intact, making this a cheap
# once-per-image-build no-op kept as insurance.
#
# Runs AFTER the cache push above, never gated: the commit strips BuildKit's
# inline-cache metadata, so the pushed ref must be the unmodified buildx output,
# and the local image always needs the fix before build.sh runs generate-zbm.
# No -q on the commit: output is discarded anyway, and the docker CLI has no
# such flag.
ctr="zbm_perlfix_$$"
docker rm -f "$ctr" >/dev/null 2>&1 || true
trap 'docker rm -f "$ctr" >/dev/null 2>&1 || true' EXIT
docker run --name "$ctr" --entrypoint /usr/bin/xbps-install "$img" -fy perl
docker commit \
  --change 'ENTRYPOINT ["/build-init.sh"]' \
  --change 'ENV DRACUT_NO_XATTR=1' \
  "$ctr" "$img" >/dev/null
docker rm -f "$ctr" >/dev/null
trap - EXIT

docker run --rm --entrypoint /usr/bin/bash "$img" -lc '
  set -euo pipefail
  command -v dropbear >/dev/null
  test -f /usr/lib/dracut/modules.d/60crypt-ssh/module-setup.sh
  test -f /usr/share/perl5/core_perl/Pod/Usage.pm
'
