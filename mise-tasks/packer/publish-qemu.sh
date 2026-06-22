#!/usr/bin/env bash
#MISE description="Build or seed a qemu fixture image and publish it to the CI image bucket"
#USAGE arg "<machine>" help="Qemu fixture machine to publish: box or box_deps"
#USAGE complete "machine" run="printf 'box\nbox_deps\n'"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="jammy"
#USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
#USAGE flag "--promote" help="After upload, write the promoted.json pointer to this build"
#USAGE flag "--dry-run" help="Build/seed normally, then print the upload plan without writing S3"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

machine=$usage_machine
ubuntu=$usage_ubuntu

case "$machine" in
box)
  mise run packer:init
  mise run packer:build box --ubuntu "$ubuntu"
  ;;
box_deps)
  mise run packer:seed-deps --ubuntu "$ubuntu"
  ;;
*)
  echo "unsupported qemu fixture machine: $machine" >&2
  exit 2
  ;;
esac

upload_args=("$machine" --ubuntu "$ubuntu")
if [ "${usage_promote:-false}" = "true" ]; then
  upload_args+=(--promote)
fi
if [ "${usage_dry_run:-false}" = "true" ]; then
  upload_args+=(--dry-run)
fi
mise run packer:upload-s3 "${upload_args[@]}"
