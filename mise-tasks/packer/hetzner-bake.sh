#!/usr/bin/env bash
#MISE description="Bake fox's Hetzner Cloud boot image on an EC2 surrogate (mechanic 1, no KVM) and publish it as a Hetzner snapshot. The build instance streams its disk straight onto a temp rescue server -- no 20G image touches the runner."
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="jammy"
#USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# shellcheck source=_hetzner_rescue.sh
source "$(dirname "$0")/_hetzner_rescue.sh"
# shellcheck source=_bake_backstop.sh
source "$(dirname "$0")/_bake_backstop.sh"

ubuntu="${usage_ubuntu}"
region=eu-central-1

# Deregister every byproduct AMI the hetzner source can leave behind, plus its
# snapshots (ebssurrogate cannot skip AMI registration; we only ever want the
# Hetzner snapshot). Driven from the EXIT trap so it runs on success AND on any
# failure that still lets the script exit -- the old success-only path orphaned
# one when a non-ASCII AMI description failed packer right after registration.
# A name sweep, not the manifest (which only exists on success), also clears an
# orphan a prior run left, so byproducts never accumulate. The name pattern is
# hetzner-only, so the kept box/pug/lab AMIs are never touched. Best-effort
# throughout: cleanup must never mask the real exit code. Runs serialized
# (resource_group: fox_image), so the sweep can't race a concurrent bake.
deregister_byproduct_amis() {
  local region="$1" ubuntu="$2" ami snaps snap
  for ami in $(aws --region "$region" ec2 describe-images --owners self \
    --filters "Name=name,Values=homelab-hetzner-${ubuntu}-*" \
    --query 'Images[].ImageId' --output text 2>/dev/null); do
    [ "$ami" = "None" ] && continue
    snaps=$(aws --region "$region" ec2 describe-images --image-ids "$ami" \
      --query 'Images[0].BlockDeviceMappings[?Ebs != null].Ebs.SnapshotId' \
      --output text 2>/dev/null) || snaps=""
    aws --region "$region" ec2 deregister-image --image-id "$ami" >/dev/null 2>&1 || {
      echo "byproduct cleanup: could not deregister $ami" >&2
      continue
    }
    for snap in $snaps; do
      [ "$snap" = "None" ] && continue
      aws --region "$region" ec2 delete-snapshot --snapshot-id "$snap" >/dev/null 2>&1 ||
        echo "byproduct cleanup: could not delete snapshot $snap" >&2
    done
    echo "==> deregistered byproduct $ami (snapshots: ${snaps:-none})"
  done
}

# --on-error=ask keeps the failed build instance up for SSH debugging when a
# human is at the terminal; CI gets cleanup so a failure can't leave a
# billing instance behind (same policy as packer:ami).
on_error=cleanup
if [ -t 0 ] && [ -z "${CI:-}" ]; then
  on_error=ask
fi

# Stand up the rescue server first so it is waiting in rescue mode when the
# bake's final provisioner streams the disk onto its /dev/sda. The trap tears
# it down on any failure (including a failed bake). It sits idle in rescue for
# the ~20min bake -- a cpx22 hour is pennies.
rescue_init
trap rescue_cleanup EXIT
rescue_create

backstop_state=$(mktemp)
backstop_pid=""
trap 'bake_backstop_disarm "$region" "$backstop_state" "$backstop_pid"; deregister_byproduct_amis "$region" "$ubuntu"; rm -rf "$backstop_state"; rescue_cleanup' EXIT

# Arm the orphan backstop (CI-only, inside the helper) in the background: the
# 90-min job timeout that orphaned a build instance here once (it killed packer
# mid-stream, skipping its cleanup) would skip the trap above too.
bake_backstop_arm "$region" "${CI_PIPELINE_ID:-local}" "hetzner" "$ubuntu" "$backstop_state" &
backstop_pid=$!

# The hetzner source's final provisioner pipes `dd | zstd` from the surrogate
# volume into the rescue server's `zstd -d | dd of=/dev/sda` (KEY authorizes
# the build instance; RESCUE_IP is its target). packer still registers a
# byproduct AMI -- ebssurrogate cannot skip it -- which the EXIT trap's
# deregister_byproduct_amis sweep drops on every path. The hetzner source runs
# no post-processor (the manifest is box/pug/lab-only), so manifest_path is left
# at its default and unused here.
packer build \
  -timestamp-ui \
  -warn-on-undeclared-var \
  "--on-error=${on_error}" \
  -only="amazon-ebssurrogate.hetzner" \
  -var "ubuntu_name=${ubuntu}" \
  -var "build_id=${CI_PIPELINE_ID:-local}" \
  -var "hetzner_rescue_ip=${RESCUE_IP}" \
  -var "hetzner_rescue_key=${KEY}" \
  packer/aws

# The disk is already written to the rescue server's /dev/sda by the bake's
# stream provisioner; turn it into the published snapshot.
rescue_snapshot "$ubuntu"
