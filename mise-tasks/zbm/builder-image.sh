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
  git clone --depth 1 --branch "v${ZBM_VERSION}" https://github.com/zbm-dev/zfsbootmenu.git "$tmp_dir"
  rm -rf "$src_dir"
  mv "$tmp_dir" "$src_dir"
  trap - EXIT INT TERM
fi

# PACKAGES are extra Void packages layered onto upstream's base image to satisfy
# recovery.conf's install_items: mdadm + nvme-cli (disk tooling), dracut-crypt-ssh
# + dropbear (recovery SSH), and dhclient for ip=single-dhcp (the base ships no
# DHCP client).
img="localhost/zbm-builder:v${ZBM_VERSION}-${arch}"

# Registry-backed layer cache in the on-lab Nexus homelab docker repo. The ref
# defaults to the shared tag here -- the single source of truth for its registry
# path + version + arch, so CI and the workstation can't drift. Set
# ZBM_BUILDER_CACHE_REF empty to opt out of caching entirely. --cache-from pulls
# prior layers so a build reuses the slow xbps-install layers even after the
# local buildkitd cache is pruned; cache import is best-effort, so a miss or an
# unauthenticated 401/404 is a non-fatal warning. --cache-to type=inline embeds
# the cache metadata into the built image (local only, needs no auth).
: "${ZBM_BUILDER_CACHE_REF:=nexus.lab.fahm.fr/homelab/zbm-builder:v${ZBM_VERSION}-${arch}}"
cache_args=()
if [ -n "$ZBM_BUILDER_CACHE_REF" ]; then
  cache_args=(--cache-from "type=registry,ref=${ZBM_BUILDER_CACHE_REF}" --cache-to type=inline)
fi
docker buildx build \
  --pull \
  --progress=plain \
  --build-arg "XBPS_REPOS=${xbps_repo}" \
  --build-arg "PACKAGES=mdadm nvme-cli dracut-crypt-ssh dropbear dhclient" \
  ${cache_args[@]+"${cache_args[@]}"} \
  --load \
  --tag "$img" \
  -f "$src_dir/releng/docker/Dockerfile" \
  "$src_dir/releng/docker"

# Push the freshly built image as the cache source for the next build (the
# expensive xbps toolchain layer is what we want cached). Gated on
# ZBM_BUILDER_CACHE_PUSH because the push needs a prior `podman login` to the
# registry: CI logs in then sets the flag; the workstation opts in explicitly
# (ZBM_BUILDER_CACHE_PUSH=1 mise run zbm:builder-image) once logged in.
if [ -n "$ZBM_BUILDER_CACHE_REF" ] && [ -n "${ZBM_BUILDER_CACHE_PUSH:-}" ]; then
  podman tag "$img" "${ZBM_BUILDER_CACHE_REF}"
  podman push "${ZBM_BUILDER_CACHE_REF}"
fi

# Work around a rootless-BuildKit unpack quirk: the build drops perl's
# /usr/share/perl5/core_perl/Pod/Usage.pm during xbps extraction (perl ships the
# file, but it lands missing from the built image while perl's other modules are
# fine), so generate-zbm dies at "use Pod::Usage" with "Can't locate Pod/Usage.pm".
# perl-Pod-Usage is only a virtual provide of perl, so it can't be pulled as a
# package -- but re-extracting perl via podman (which unpacks it correctly)
# restores the file. The quirk is specific to the rootless buildkitd builder the
# CI zbm_build step uses; a Mac podman-machine buildx build unpacks perl intact,
# so this re-extract is a once-per-image-build no-op there.
#
# Runs AFTER the cache push above, never gated: podman commit strips BuildKit's
# inline-cache metadata, so the pushed ref must be the unmodified buildx output,
# and the local image always needs the fix before build.sh runs generate-zbm.
podman rm -f zbm_perlfix >/dev/null 2>&1 || true
podman run --name zbm_perlfix --entrypoint /usr/bin/xbps-install "$img" -fy perl
podman commit -q zbm_perlfix "$img" >/dev/null
podman rm -f zbm_perlfix >/dev/null
