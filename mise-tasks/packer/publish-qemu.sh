#!/usr/bin/env bash
#MISE description="Build or seed a qemu fixture image and publish it to the CI image bucket"
#USAGE arg "<machine>" help="Qemu fixture machine to publish: box, box_deps, or lab"
#USAGE complete "machine" run="printf 'box\nbox_deps\nlab\n'"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="noble"
#USAGE complete "ubuntu" run="yq -r '.releases | keys | .[]' data/ubuntu_releases.yml"
#USAGE flag "--bucket <bucket>" help="S3 bucket for qemu image bundles" default="homelab-ci-images"
#USAGE flag "--region <region>" help="AWS region for S3" default="eu-central-1"
#USAGE flag "--architecture <architecture>" help="Guest architecture (x86_64 or aarch64)" default="x86_64"
#USAGE flag "--promote" help="After upload, write the promoted.json pointer to this build"
#USAGE flag "--dry-run" help="Build/seed normally, then print the upload plan without writing S3"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

machine=$usage_machine
ubuntu=$usage_ubuntu
bucket=${usage_bucket:-homelab-ci-images}
region=${usage_region:-eu-central-1}
architecture=${usage_architecture:-x86_64}

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
  mise run packer:build "$machine" --ubuntu "$ubuntu"
  ;;
box_deps)
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
if [ "${usage_promote:-false}" = "true" ]; then
  upload_args+=(--promote)
fi
if [ "${usage_dry_run:-false}" = "true" ]; then
  upload_args+=(--dry-run)
fi
mise run packer:upload-s3 "${upload_args[@]}"
