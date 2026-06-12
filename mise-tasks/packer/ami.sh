#!/usr/bin/env bash
#MISE description="Bake an EC2 test-cell AMI (amazon-ebssurrogate; no KVM needed) and optionally promote it"
#MISE interactive=true
#USAGE arg "<machine>" help="Machine to bake: box, pug, or lab"
#USAGE complete "machine" run="printf 'box\npug\nlab\n'"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="jammy"
#USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
#USAGE flag "--promote" help="After a successful bake + mapping check, write the AMI id to the /homelab-ci/ami/<machine>/<ubuntu> SSM parameter the harness resolves. Without it the bake prints the candidate AMI id and the promote command."
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

region=eu-central-1
machine="${usage_machine}"
ubuntu="${usage_ubuntu}"

case "$machine" in
box | pug | lab) ;;
*)
  echo "Error: machine must be box, pug, or lab (got '$machine')" >&2
  exit 1
  ;;
esac

# --on-error=ask keeps the failed build instance up for SSH debugging when a
# human is at the terminal; CI gets cleanup so a failure can't leave a
# billing instance behind (same policy as packer:build).
on_error=cleanup
if [ -t 0 ] && [ -z "${CI:-}" ]; then
  on_error=ask
fi

packer init packer/aws

manifest=$(mktemp)
trap 'rm -f "$manifest"' EXIT
rm -f "$manifest" # manifest post-processor refuses to overwrite a non-manifest file

packer build \
  -timestamp-ui \
  -warn-on-undeclared-var \
  "--on-error=${on_error}" \
  -only="amazon-ebssurrogate.${machine}" \
  -var "ubuntu_name=${ubuntu}" \
  -var "build_id=${CI_PIPELINE_ID:-local}" \
  -var "manifest_path=${manifest}" \
  packer/aws

ami=$(python3 -c '
import json, sys
manifest = json.load(open(sys.argv[1]))
print(manifest["builds"][-1]["artifact_id"].split(":")[1])
' "$manifest")
echo "==> Baked ${ami}"

# Functional check on the registered mapping: a non-root volume that misses
# DeleteOnTermination=true would orphan an EBS volume after every cell
# termination — the one failure mode that bills forever (design note: every
# EBS mapping, including non-root disks, sets DeleteOnTermination=true).
# shellcheck disable=SC2016  # the backticked `false` is JMESPath syntax, not shell
leaky=$(aws --region "$region" ec2 describe-images --image-ids "$ami" \
  --query 'Images[0].BlockDeviceMappings[?Ebs != null && Ebs.DeleteOnTermination == `false`].DeviceName' \
  --output text)
if [ -n "$leaky" ]; then
  echo "Error: AMI ${ami} has mappings without DeleteOnTermination=true: ${leaky}" >&2
  echo "Deregister it and fix packer/aws/ami.pkr.hcl before promoting." >&2
  exit 1
fi
echo "==> Every EBS mapping sets DeleteOnTermination=true"

param="/homelab-ci/ami/${machine}/${ubuntu}"
if [ "${usage_promote:-false}" != "true" ]; then
  echo "==> Candidate only (no --promote). Smoke it, then promote with:"
  echo "    aws --region ${region} ssm put-parameter --name ${param} --type String --value ${ami} --overwrite"
  exit 0
fi

aws --region "$region" ssm put-parameter \
  --name "$param" \
  --type String \
  --value "$ami" \
  --overwrite
echo "==> Promoted ${ami} to ${param}"
echo "    Rollback: re-put the previous value (aws ssm get-parameter-history --name ${param})"
