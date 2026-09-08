#!/usr/bin/env bash
#MISE description="Build and publish the ZFSBootMenu CI artifact"
set -euo pipefail

# shellcheck source=mise-tasks/zbm/lib.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

arch="$(zbm_host_arch)"

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

if [ "$CI_COMMIT_BRANCH" = "$CI_DEFAULT_BRANCH" ]; then
  mise run zbm:upload
fi
