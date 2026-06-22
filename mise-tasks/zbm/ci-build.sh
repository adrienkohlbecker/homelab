#!/usr/bin/env bash
#MISE description="Build and publish the ZFSBootMenu CI artifact"
set -euo pipefail

# shellcheck source=mise-tasks/zbm/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

: "${CI_PIPELINE_ID:?CI_PIPELINE_ID is required}"
: "${CI_JOB_ID:?CI_JOB_ID is required}"
: "${CI_REGISTRY_IMAGE:?CI_REGISTRY_IMAGE is required}"
: "${CI_REGISTRY:?CI_REGISTRY is required}"
: "${CI_REGISTRY_USER:?CI_REGISTRY_USER is required}"
: "${CI_REGISTRY_PASSWORD:?CI_REGISTRY_PASSWORD is required}"
: "${CI_COMMIT_BRANCH:?CI_COMMIT_BRANCH is required}"
: "${CI_DEFAULT_BRANCH:?CI_DEFAULT_BRANCH is required}"
: "${DOCKER_HOST:?DOCKER_HOST is required}"
: "${DOCKER_CERT_PATH:?DOCKER_CERT_PATH is required}"

repo_root="$(zbm_repo_root)"
arch="$(zbm_host_arch)"

# Resolve through mise, not by parsing mise.toml: these vars are tera
# conditionals whose embedded quoted literals defeat naive text extraction.
eval "$(mise env)"
if [ -z "${ZBM_VERSION:-}" ] || [ -z "${ZBM_KERNEL_VERSION:-}" ]; then
  echo "ZBM_VERSION/ZBM_KERNEL_VERSION not set in mise env" >&2
  exit 1
fi

timeout 60 bash -c 'until docker info >/dev/null 2>&1; do sleep 1; done'
docker context create dind --docker "host=${DOCKER_HOST},ca=${DOCKER_CERT_PATH}/ca.pem,cert=${DOCKER_CERT_PATH}/cert.pem,key=${DOCKER_CERT_PATH}/key.pem"
docker buildx create dind --name zbm --driver docker-container --use
docker buildx inspect --bootstrap zbm
echo "$CI_REGISTRY_PASSWORD" | docker login -u "$CI_REGISTRY_USER" --password-stdin "$CI_REGISTRY"

export ZBM_BUILD_SUFFIX="-ci.${CI_PIPELINE_ID}.${CI_JOB_ID}"
export ZBM_BUILDER_CACHE_REF="${CI_REGISTRY_IMAGE}/zbm-builder:v${ZBM_VERSION}-${arch}"
echo "Building ZFSBootMenu v${ZBM_VERSION}${ZBM_BUILD_SUFFIX} for ${arch}"
mise run zbm:builder-image
mise run zbm:build

tarball="${repo_root}/zbm-build/${arch}/zfsbootmenu-v${ZBM_VERSION}-linux${ZBM_KERNEL_VERSION}${ZBM_BUILD_SUFFIX}-${arch}.tar.gz"
if [ ! -f "$tarball" ]; then
  echo "tarball not produced: $tarball" >&2
  exit 1
fi

members="$(tar tzf "$tarball")"
echo "$members"
for f in initramfs-bootmenu.img zfsbootmenu.EFI ssh_host_ed25519_key.pub; do
  grep -qxF "$f" <<<"$members" || {
    echo "missing $f in tarball" >&2
    exit 1
  }
done
grep -qE '^vmlin.*-bootmenu$' <<<"$members" || {
  echo "missing vmlin*-bootmenu kernel in tarball" >&2
  exit 1
}
echo "ZBM tarball OK: $tarball"

if [ "$CI_COMMIT_BRANCH" = "$CI_DEFAULT_BRANCH" ]; then
  mise run zbm:upload
fi
