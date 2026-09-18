#!/usr/bin/env bash
#MISE description="Build a qemu fixture image and publish it to the CI image bucket"
#USAGE arg "<machine>" help="Qemu fixture machine to publish: lab or pug"
#USAGE complete "machine" run="printf 'lab\npug\n'"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="noble"
#USAGE complete "ubuntu" run="yq -r '.releases | keys | .[]' data/ubuntu_releases.yml"
#USAGE flag "--architecture <architecture>" help="Guest architecture (x86_64 or aarch64); must match this build host, which is the default, and selects the image store"
#USAGE flag "--upstream" help="Pull apt packages and the cloud image from upstream Ubuntu mirrors during the build"
#USAGE flag "--promote" help="After upload, write the promoted.json pointer to this build"
#USAGE flag "--dry-run" help="Build normally, then print the upload plan without writing S3"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

machine=$usage_machine
ubuntu=$usage_ubuntu
architecture=${usage_architecture:-$(uname -m)}

case "$architecture" in
arm64) architecture=aarch64 ;;
x86_64 | aarch64) ;;
*)
  echo "unsupported qemu fixture architecture: $architecture" >&2
  exit 2
  ;;
esac

case "$machine" in
lab | pug) ;;
*)
  echo "unsupported qemu fixture machine: $machine" >&2
  exit 2
  ;;
esac

upload_args=(
  "$machine"
  --ubuntu "$ubuntu"
  --architecture "$architecture"
)
if [ "${usage_promote:-false}" = "true" ]; then
  upload_args+=(--promote)
fi
if [ "${usage_dry_run:-false}" = "true" ]; then
  upload_args+=(--dry-run)
fi

# Reject a mismatched architecture before spending a build on it.
mise run packer:upload-s3 "${upload_args[@]}" --preflight

mise run packer:init
build_args=("$machine" --ubuntu "$ubuntu")
if [ "${usage_upstream:-false}" = "true" ]; then
  build_args+=(--upstream)
fi
mise run packer:build "${build_args[@]}"

mise run packer:upload-s3 "${upload_args[@]}"
