#!/usr/bin/env bash
#MISE description="Bake the Hetzner Cloud boot image on an EC2 surrogate volume (mechanic 1, no KVM) and download it as a raw.gz. Upload it with `mise run packer:hetzner`."
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="jammy"
#USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

ubuntu="${usage_ubuntu}"
out="packer/artifacts/hetzner/${ubuntu}.raw.gz"

# --on-error=ask keeps the failed build instance up for SSH debugging when a
# human is at the terminal; CI gets cleanup so a failure can't leave a
# billing instance behind (same policy as packer:ami).
on_error=cleanup
if [ -t 0 ] && [ -z "${CI:-}" ]; then
  on_error=ask
fi

region=eu-central-1

packer init packer/aws

manifest=$(mktemp)
trap 'rm -rf "$manifest"' EXIT
rm -f "$manifest" # manifest post-processor refuses to overwrite a non-manifest file

mkdir -p "$(dirname "$out")"
rm -f "$out"

packer build \
  -timestamp-ui \
  -warn-on-undeclared-var \
  "--on-error=${on_error}" \
  -only="amazon-ebssurrogate.hetzner" \
  -var "ubuntu_name=${ubuntu}" \
  -var "build_id=${CI_PIPELINE_ID:-local}" \
  -var "manifest_path=${manifest}" \
  -var "hetzner_image_path=${out}" \
  packer/aws

# The artifact is the downloaded raw.gz; the registered AMI is a byproduct
# (ebssurrogate cannot skip registration). Drop it — snapshot first lookup,
# then deregister — before asserting the download, so a failed download
# never leaves a stray AMI billing for its snapshot.
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

# Turn "packer exited 0" into an assertion on the artifact this task is for.
test -s "$out" || {
  echo "image not downloaded: $out" >&2
  exit 1
}
gzip -t "$out"

echo "==> Baked $out ($(du -h "$out" | cut -f1))"
echo "    Publish: mise run packer:hetzner -- $out --ubuntu $ubuntu"
