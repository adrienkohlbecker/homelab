#!/usr/bin/env bash
#MISE description="Upload a pre-built ZFS-root disk image to a Hetzner Cloud snapshot. Build the image first with `mise run packer:build hetzner` (qemu/KVM); this streams that raw image onto a throwaway Hetzner rescue server and snapshots it."
#USAGE arg "[image]" help="Path to the rpool disk image, raw or raw.gz (default: the packer:build hetzner artifact for --ubuntu)"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu codename -- snapshot label + default image path" default="jammy"
#USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

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
# (mbuffer | zstd -dc | dd). Compress with zstd here (parallel via -T0; -1
# since the payload is already-zstd'd rpool blocks + zeros, so speed beats
# ratio); a .zst input passes through, a legacy .gz is transcoded. mbuffer on
# the send side, when present, smooths the handoff into ssh.
echo "==> streaming $IMG ($(du -h "$IMG" | cut -f1)) onto /dev/sda (this takes a few minutes)"
sender() {
  case "$IMG" in
  *.zst) cat "$IMG" ;;
  *.gz) gzip -dc "$IMG" | zstd -1 -T0 ;;
  *) zstd -1 -T0 -c "$IMG" ;;
  esac
}
if command -v mbuffer >/dev/null; then
  sender | mbuffer -m 512M | ssh_rescue "$RESCUE_RECV"
else
  sender | ssh_rescue "$RESCUE_RECV"
fi

rescue_snapshot "$UBUNTU"
