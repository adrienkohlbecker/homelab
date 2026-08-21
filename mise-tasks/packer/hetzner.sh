#!/usr/bin/env bash
#MISE description="Upload a pre-built ZFS-root disk image to a Hetzner Cloud snapshot. Build the image first with `mise run packer:build hetzner` (qemu/KVM); this streams that raw image onto a throwaway Hetzner rescue server and snapshots it."
#USAGE arg "[image]" help="Path to the raw rpool disk image (default: the packer:build hetzner artifact for --ubuntu)"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu codename -- snapshot label + default image path" default="noble"
#USAGE complete "ubuntu" run="printf 'noble\nresolute\n'"
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

# The rescue server was created from a stock ubuntu-24.04 image, so /dev/sda
# already carries that image's GPT (backup header at the true ~76G disk end)
# and its filesystems. We stream a smaller raw image onto the front with
# conv=sparse, which never touches the tail -- leaving a stale backup GPT and
# stale partitions past the image end that disagree with its own primary
# GPT and can block the firmware from booting the snapshot. Discard the whole
# device first so only the streamed image's structures survive; the in-rescue
# install path (provision.sh) wipes equivalently before partitioning. Fall back
# to wipefs if the device rejects discard -- the post-stream sgdisk -e below
# rewrites the backup header regardless, so a discard failure is non-fatal.
echo "==> wiping /dev/sda before streaming (clears the rescue image's stale GPT + tail)"
ssh_rescue 'blkdiscard -f /dev/sda || wipefs -a /dev/sda'

# Stream the raw image onto /dev/sda via the shared rescue receive pipeline.
# Compress with zstd here; the rpool blocks are already compressed, so speed
# beats ratio. mbuffer on the send side, when present, smooths the ssh handoff.
echo "==> streaming $IMG ($(du -h "$IMG" | cut -f1)) onto /dev/sda (this takes a few minutes)"
if command -v mbuffer >/dev/null; then
  zstd -1 -T0 -c "$IMG" | mbuffer -m 512M | ssh_rescue_bulk "$RESCUE_RECV"
else
  zstd -1 -T0 -c "$IMG" | ssh_rescue_bulk "$RESCUE_RECV"
fi

# The streamed image's GPT backup header sits at the image's own end, not the
# true ~76G disk end the firmware expects. Relocate it so the GPT
# is consistent with the real disk and the firmware boots the snapshot cleanly.
# hetzner_growpart.service relocates it too, but only after a successful boot --
# fixing it here keeps a misplaced backup header from blocking that boot.
echo "==> relocating the GPT backup header to the disk end"
ssh_rescue 'sgdisk -e /dev/sda'

# Read the streamed GPT directly rather than relying on the rescue kernel's
# stale partition nodes, then refuse to publish an image that lost the
# rebuild-only Podman partition. The boot verifier below proves the OS comes
# up; this check proves the raw device the Podman role will format on first
# converge has the required label and size.
echo "==> verifying the dedicated Podman partition"
ssh_rescue 'sgdisk -i 4 /dev/sda | grep -Eq "^Partition name: .podman.$" && sgdisk -i 4 /dev/sda | grep -Fq "40.0 GiB"'

rescue_snapshot "$UBUNTU"
