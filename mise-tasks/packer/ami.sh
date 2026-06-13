#!/usr/bin/env bash
#MISE description="Bake an EC2 test-cell AMI (amazon-ebssurrogate; no KVM needed) and optionally promote it"
#MISE interactive=true
#USAGE arg "<machine>" help="Machine to bake: box, pug, lab, or box_deps (derived from the promoted box AMI)"
#USAGE complete "machine" run="printf 'box\npug\nlab\nbox_deps\n'"
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="jammy"
#USAGE complete "ubuntu" run="printf 'jammy\nnoble\nresolute\n'"
#USAGE flag "--promote" help="After a successful bake + mapping check, write the AMI id to the /homelab-ci/ami/<machine>/<ubuntu> SSM parameter the harness resolves. Without it the bake prints the candidate AMI id and the promote command."
#USAGE flag "--ssh-key <path>" help="box_deps only: private key authorized on the box AMI (CI passes the CI_CELL_SSH_KEY file variable; locally the ssh agent supplies the operator identity)"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

region=eu-central-1
machine="${usage_machine}"
ubuntu="${usage_ubuntu}"

case "$machine" in
box | pug | lab | box_deps) ;;
*)
  echo "Error: machine must be box, pug, lab, or box_deps (got '$machine')" >&2
  exit 1
  ;;
esac

# box/pug/lab install from scratch under the ebssurrogate builder; box_deps
# boots the promoted box AMI and converges packer/seed_deps.yml onto it
# under amazon-ebs (see the source comment in packer/aws/ami.pkr.hcl).
only="amazon-ebssurrogate.${machine}"
extra_vars=()
if [ "$machine" = "box_deps" ]; then
  only="amazon-ebs.box_deps"
  # Resolve the parent here rather than in HCL so the derivation always
  # starts from the *promoted* box, never a most-recent candidate.
  src_ami=$(aws --region "$region" ssm get-parameter \
    --name "/homelab-ci/ami/box/${ubuntu}" \
    --query Parameter.Value --output text)
  echo "==> Deriving from promoted box AMI ${src_ami}"
  extra_vars+=(-var "box_deps_source_ami=${src_ami}")
  if [ -n "${usage_ssh_key:-}" ]; then
    # GitLab file-type CI/CD variables land group/world-readable; ssh
    # refuses unprotected private keys.
    chmod 600 "${usage_ssh_key}"
    extra_vars+=(-var "cell_ssh_key=${usage_ssh_key}")
  fi
  # The seed converge runs bare ansible-playbook (packer's ansible
  # provisioner), outside the harness that normally repairs the
  # .ansible-mitogen-strategy symlink ansible.cfg points at.
  uv run python test/setup_mitogen.py
fi

# --on-error=ask keeps the failed build instance up for SSH debugging when a
# human is at the terminal; CI gets cleanup so a failure can't leave a
# billing instance behind (same policy as packer:build).
on_error=cleanup
if [ -t 0 ] && [ -z "${CI:-}" ]; then
  on_error=ask
fi

manifest=$(mktemp)
trap 'rm -rf "$manifest"' EXIT
rm -f "$manifest" # manifest post-processor refuses to overwrite a non-manifest file

packer build \
  -timestamp-ui \
  -warn-on-undeclared-var \
  "--on-error=${on_error}" \
  -only="${only}" \
  -var "ubuntu_name=${ubuntu}" \
  -var "build_id=${CI_PIPELINE_ID:-local}" \
  -var "manifest_path=${manifest}" \
  ${extra_vars[@]+"${extra_vars[@]}"} \
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
  echo "    aws --region ${region} ssm put-parameter --name ${param} --type String --data-type aws:ec2:image --value ${ami} --overwrite"
  exit 0
fi

# data-type aws:ec2:image has EC2 validate the value is a real, available
# AMI at write time, so a bad promotion fails here, not at cell launch.
aws --region "$region" ssm put-parameter \
  --name "$param" \
  --type String \
  --data-type aws:ec2:image \
  --value "$ami" \
  --overwrite
echo "==> Promoted ${ami} to ${param}"
echo "    Rollback: re-put the previous value (aws ssm get-parameter-history --name ${param})"
