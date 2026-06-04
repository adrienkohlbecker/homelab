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

# PACKAGES are extra Void packages layered onto upstream's base image. Most
# satisfy recovery.conf's install_items: mdadm + nvme-cli (disk tooling),
# dracut-crypt-ssh + dropbear (recovery SSH), dhclient for ip=single-dhcp (the
# base ships no DHCP client). perl-Pod-Usage is a generate-zbm *runtime* dep:
# recent perl dropped Pod::Usage from core and zfsbootmenu's run_depends don't
# pull it, so the build dies with "Can't locate Pod/Usage.pm" without it.
docker buildx build \
  --pull \
  --progress=plain \
  --build-arg "XBPS_REPOS=${xbps_repo}" \
  --build-arg "PACKAGES=mdadm nvme-cli dracut-crypt-ssh dropbear dhclient perl-Pod-Usage" \
  --load \
  --tag "localhost/zbm-builder:v${ZBM_VERSION}-${arch}" \
  -f "$src_dir/releng/docker/Dockerfile" \
  "$src_dir/releng/docker"
