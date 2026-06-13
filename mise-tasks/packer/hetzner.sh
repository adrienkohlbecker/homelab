#!/usr/bin/env bash
#MISE description="Upload a pre-built ZFS-root disk image to a Hetzner Cloud snapshot (mechanic 2). Build the image first with `mise run packer:build hetzner` (qemu/KVM). For the EC2 surrogate, use `mise run packer:hetzner-bake`, which bakes and publishes in one step."
#USAGE arg "[image]" help="Path to the rpool disk image, raw or raw.gz (default: the packer:build hetzner artifact for --ubuntu)"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu codename -- snapshot label + default image path" default="jammy"
#USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# shellcheck source=_hcloud_token.sh
source "$(dirname "$0")/_hcloud_token.sh"
# shellcheck source=_hetzner_rescue.sh
source "$(dirname "$0")/_hetzner_rescue.sh"

UBUNTU="$usage_ubuntu"
# Default to the artifact `mise run packer:build hetzner` publishes (raw, on
# lab). The upload streams the image straight onto /dev/sda, so it must be a
# raw disk image, not a qcow2 -- pass an explicit path if it lives elsewhere.
IMG="${usage_image:-${HOMELAB_CI_DIR}/${UBUNTU}/hetzner/packer-ubuntu-1.raw}"
[ -f "$IMG" ] || {
  echo "no disk image at $IMG -- build it first: mise run packer:build hetzner" >&2
  exit 1
}

rescue_init
trap rescue_cleanup EXIT
rescue_create

# Stream the image onto /dev/sda via the shared rescue receive pipeline
# (mbuffer | pigz -d | dd). A .raw.gz is already wire-ready; a raw input is
# compressed here -- pigz (parallel) when present, else gzip. mbuffer on the
# send side, when present, smooths the handoff into ssh. Whatever this runner
# has, the rescue always decodes it (pigz -d reads gzip too).
echo "==> streaming $IMG ($(du -h "$IMG" | cut -f1)) onto /dev/sda (this takes a few minutes)"
sender() {
  case "$IMG" in
  *.gz) cat "$IMG" ;;
  *) if command -v pigz >/dev/null; then pigz -c "$IMG"; else gzip -c "$IMG"; fi ;;
  esac
}
if command -v mbuffer >/dev/null; then
  sender | mbuffer -m 512M | ssh_rescue "$RESCUE_RECV"
else
  sender | ssh_rescue "$RESCUE_RECV"
fi

rescue_snapshot "$UBUNTU"
