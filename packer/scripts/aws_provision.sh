#!/bin/bash
# Entry point for the AWS ebssurrogate bake (packer/aws/ami.pkr.hcl):
# resolve the EBS block-device-mapping names packer declares (/dev/xvdf...)
# to the kernel devices the Nitro build instance actually exposes
# (/dev/nvme*n1), then hand off to the shared provision.sh.
#
# Why resolution is needed: on Nitro, EBS volumes surface as NVMe
# namespaces in nondeterministic probe order — /dev/xvdf does not exist
# and nvme indices don't follow mapping order. The EBS NVMe device
# advertises its mapping name in the identify-controller vendor-specific
# field (bytes 3072-3103), which is what amazon's own ebsnvme-id reads.
#
# Inputs: the same env block provision.sh documents, with DISKS /
# EXTRA_DISKS carrying mapping names. Exports the resolved paths and
# SCRIPTS_DIR before exec'ing provision.sh.
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive

# cloud-init's config stage races apt below on a fresh cloud instance —
# same wait provision.sh does before its own installs.
cloud-init status --wait || true

apt-get update
apt-get install --yes nvme-cli

# resolve <mapping-name>: print the kernel device whose EBS vendor field
# matches. Mapping names appear with or without the /dev/ prefix
# depending on firmware; compare both. Passes through paths that already
# exist as block devices (a Xen build instance exposes /dev/xvd*
# directly).
resolve() {
  local want="$1" dev name raw
  if [ -b "$want" ]; then
    echo "$want"
    return 0
  fi
  raw=$(mktemp)
  for dev in /dev/nvme*n1; do
    nvme id-ctrl --raw-binary "$dev" >"$raw" 2>/dev/null || continue
    name=$(dd if="$raw" bs=1 skip=3072 count=32 status=none | tr -d " \0")
    name="/dev/${name#/dev/}"
    if [ "$name" = "$want" ]; then
      rm -f "$raw"
      echo "$dev"
      return 0
    fi
  done
  rm -f "$raw"
  echo >&2 "aws_provision.sh: no NVMe device advertises mapping name $want"
  return 1
}

resolve_list() {
  local out="" d
  for d in $1; do
    out+="${out:+ }$(resolve "$d")"
  done
  echo "$out"
}

DISKS=$(resolve_list "$DISKS")
EXTRA_DISKS=$(resolve_list "${EXTRA_DISKS:-}")
export DISKS EXTRA_DISKS

SCRIPTS_DIR=$(cd "$(dirname "$0")" && pwd)
export SCRIPTS_DIR

# Surface the resolved kernel devices for follow-up provisioners: the
# hetzner image export (packer/aws/ami.pkr.hcl) reads the rpool disk back
# off the instance and only knows the mapping name.
echo "$DISKS" >"$SCRIPTS_DIR/resolved_disks"

# EC2 cells boot unwatched via the firmware-fallback rEFInd (empty NVRAM),
# so the menu countdown is pure dead time — measured ~6s per boot on a
# box/jammy cell. -1 boots the default selection immediately; the menu
# stays reachable by holding a key during rEFInd startup on the
# interactive EC2 serial console. The hetzner image keeps the countdown:
# on fox it's the operator's window to reach ZBM/recovery from the
# Hetzner console.
if [ "${IMAGE_TARGET:-qemu}" != "hetzner" ]; then
  export REFIND_TIMEOUT=-1
fi

exec bash "$SCRIPTS_DIR/provision.sh"
