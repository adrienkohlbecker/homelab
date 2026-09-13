#!/usr/bin/env bash
#MISE description="Build or seed a qemu fixture image and publish it to the CI image bucket"
#USAGE arg "<machine>" help="Qemu fixture machine to publish: box, box_deps, or lab"
#USAGE complete "machine" run="printf 'box\nbox_deps\nlab\n'"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="noble"
#USAGE complete "ubuntu" run="yq -r '.releases | keys | .[]' data/ubuntu_releases.yml"
#USAGE flag "--bucket <bucket>" help="S3 bucket for qemu image bundles" default="homelab-ci-images"
#USAGE flag "--region <region>" help="AWS region for S3" default="eu-central-1"
#USAGE flag "--architecture <architecture>" help="Guest architecture (x86_64 or aarch64)" default="x86_64"
#USAGE flag "--build-id <build_id>" help="Immutable S3 build id; defaults inside upload-s3.py"
#USAGE flag "--base-build-id <base_build_id>" help="Exact box build to hydrate before an aarch64 box_deps build"
#USAGE flag "--promote" help="After upload, write the promoted.json pointer to this build"
#USAGE flag "--dry-run" help="Build/seed normally, then print the upload plan without writing S3"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

machine=$usage_machine
ubuntu=$usage_ubuntu
bucket=${usage_bucket:-homelab-ci-images}
region=${usage_region:-eu-central-1}
architecture=${usage_architecture:-x86_64}
build_id=${usage_build_id:-}
base_build_id=${usage_base_build_id:-}

case "$architecture" in
x86_64 | aarch64) ;;
*)
  echo "unsupported qemu fixture architecture: $architecture" >&2
  exit 2
  ;;
esac

case "$machine" in
box | lab)
  mise run packer:init
  build_args=("$machine" --ubuntu "$ubuntu")
  if [ "$machine" = box ] && [ "$architecture" = aarch64 ]; then
    build_args+=(--upstream)
  fi
  mise run packer:build "${build_args[@]}"
  ;;
box_deps)
  if [ "$architecture" = aarch64 ]; then
    if [ -z "$base_build_id" ]; then
      echo "--base-build-id is required for an aarch64 box_deps build" >&2
      exit 2
    fi
    if [ -z "${CI_COMMIT_SHA:-}" ]; then
      echo "CI_COMMIT_SHA is required for an aarch64 box_deps build" >&2
      exit 2
    fi
    mise run ci:hydrate-qemu-images box \
      --ubuntu "$ubuntu" \
      --bucket "$bucket" \
      --region "$region" \
      --architecture "$architecture" \
      --build-id "$base_build_id"
    export HOMELAB_BOX_BASE_BUILD_ID="$base_build_id"
    export HOMELAB_BOX_BASE_SOURCE_SHA="$CI_COMMIT_SHA"
    export HOMELAB_BOX_BASE_ARCHITECTURE="$architecture"
    export HOMELAB_TEST_IN_AWS=true
    export HOMELAB_TEST_AWS_COMPUTE_REGION="$region"
    export HOMELAB_TEST_AWS_ECR_REGION="$region"
  fi
  mise run test:build_box_deps --ubuntu "$ubuntu"
  ;;
*)
  echo "unsupported qemu fixture machine: $machine" >&2
  exit 2
  ;;
esac

upload_args=(
  "$machine"
  --ubuntu "$ubuntu"
  --bucket "$bucket"
  --region "$region"
  --architecture "$architecture"
)
if [ -n "$build_id" ]; then
  upload_args+=(--build-id "$build_id")
fi
if [ "${usage_promote:-false}" = "true" ]; then
  upload_args+=(--promote)
fi
if [ "${usage_dry_run:-false}" = "true" ]; then
  upload_args+=(--dry-run)
fi
mise run packer:upload-s3 "${upload_args[@]}"
