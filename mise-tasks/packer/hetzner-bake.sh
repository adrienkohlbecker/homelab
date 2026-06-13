#!/usr/bin/env bash
#MISE description="Bake fox's Hetzner Cloud boot image on an EC2 surrogate (mechanic 1, no KVM) and publish it as a Hetzner snapshot. The build instance streams its disk straight onto a temp rescue server -- no 20G image touches the runner."
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="jammy"
#USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# shellcheck source=_hcloud_token.sh
source "$(dirname "$0")/_hcloud_token.sh"
# shellcheck source=_hetzner_rescue.sh
source "$(dirname "$0")/_hetzner_rescue.sh"

ubuntu="${usage_ubuntu}"
region=eu-central-1

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

packer init packer/aws

manifest=$(mktemp)
trap 'rm -rf "$manifest"; rescue_cleanup' EXIT
rm -f "$manifest" # manifest post-processor refuses to overwrite a non-manifest file

# The hetzner source's final provisioner pipes `dd | gzip` from the surrogate
# volume into the rescue server's `gunzip | dd of=/dev/sda` (KEY authorizes
# the build instance; RESCUE_IP is its target). packer still registers a
# byproduct AMI -- ebssurrogate cannot skip it -- which we drop below.
packer build \
  -timestamp-ui \
  -warn-on-undeclared-var \
  "--on-error=${on_error}" \
  -only="amazon-ebssurrogate.hetzner" \
  -var "ubuntu_name=${ubuntu}" \
  -var "build_id=${CI_PIPELINE_ID:-local}" \
  -var "manifest_path=${manifest}" \
  -var "hetzner_rescue_ip=${RESCUE_IP}" \
  -var "hetzner_rescue_key=${KEY}" \
  packer/aws

# Drop the byproduct AMI -- snapshot lookup first, then deregister -- so a
# stray AMI never bills for its snapshot after the disk is already on Hetzner.
ami=$(python3 -c '
import json, sys
manifest = json.load(open(sys.argv[1]))
print(manifest["builds"][-1]["artifact_id"].split(":")[1])
' "$manifest")
snapshots=$(aws --region "$region" ec2 describe-images --image-ids "$ami" \
  --query 'Images[0].BlockDeviceMappings[?Ebs != null].Ebs.SnapshotId' --output text)
aws --region "$region" ec2 deregister-image --image-id "$ami"
for snap in $snapshots; do
  aws --region "$region" ec2 delete-snapshot --snapshot-id "$snap"
done
echo "==> Deregistered byproduct ${ami} (snapshots: ${snapshots:-none})"

# The disk is already written to the rescue server's /dev/sda by the bake's
# stream provisioner; turn it into the published snapshot.
rescue_snapshot "$ubuntu"
