#!/usr/bin/env bash
#MISE description="Bake the AWS nested-qemu runner-host AMI and optionally promote it"
#MISE interactive=true
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="noble"
#USAGE complete "ubuntu" run="yq -r '.releases | keys | .[]' data/ubuntu_releases.yml"
#USAGE flag "--architecture <architecture>" help="Target architecture (x86_64 or aarch64); selects the region from data/architectures.yml" default="x86_64"
#USAGE flag "--promote" help="After a successful bake, update the architecture-specific qemu-host AMI pointer"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

ubuntu="${usage_ubuntu:-noble}"
architecture="${usage_architecture:-x86_64}"
build_id="${CI_PIPELINE_ID:-local}"
repo_root=$(git rev-parse --show-toplevel)

architectures="${repo_root}/data/architectures.yml"
if ! ARCH="$architecture" yq -e '.[strenv(ARCH)].ci' "$architectures" >/dev/null 2>&1; then
  echo "qemu-host-ami: unsupported architecture ${architecture}" >&2
  exit 2
fi
ci_value() {
  ARCH="$architecture" yq -r ".[strenv(ARCH)].ci.$1" "$architectures"
}
region=$(ci_value aws_region)
name_prefix=$(ci_value ami_name_prefix)
param_template=$(ci_value ami_parameter)
param=${param_template//\{ubuntu\}/$ubuntu}

promoted_ami() {
  local error_file value rc
  error_file=$(mktemp)
  if value=$(aws --region "$region" ssm get-parameter \
    --name "$param" \
    --query Parameter.Value --output text 2>"$error_file"); then
    rm -f "$error_file"
    printf '%s\n' "$value"
    return 0
  else
    rc=$?
  fi

  if grep -q ParameterNotFound "$error_file"; then
    rm -f "$error_file"
    return 0
  fi
  cat "$error_file" >&2
  rm -f "$error_file"
  return "$rc"
}

# Keep the promoted image plus the newest two provenance-tagged builds. The
# shared planner also drives ci:audit-aws, so the two paths cannot disagree.
prune_old_amis() {
  local name_tag="${name_prefix}-${ubuntu}" promoted stale image_id current matches
  local -a retention_args=()
  local -a image_filters=(
    "Name=tag:Name,Values=${name_tag}"
    "Name=tag:role,Values=ci-ami"
    "Name=tag:machine,Values=qemu_host"
    "Name=tag:ubuntu,Values=${ubuntu}"
  )
  if [ "$architecture" = aarch64 ]; then
    image_filters+=("Name=tag:architecture,Values=${architecture}")
  fi
  promoted=$(promoted_ami)
  if [ -n "$promoted" ]; then
    retention_args+=(--protected "$promoted")
  fi

  stale=$(aws --region "$region" ec2 describe-images --owners self \
    --filters "${image_filters[@]}" \
    --query Images --output json |
    python3 "$repo_root/mise-tasks/ci/ami_retention.py" "${retention_args[@]}")

  if [ -z "$stale" ]; then
    echo "==> Prune: nothing to remove (<= 2 AMIs in ${name_tag})"
    return 0
  fi

  while IFS= read -r image_id; do
    [ -n "$image_id" ] || continue

    # Promotion and tags can change after the initial list. Re-read both at the
    # destructive boundary and abort rather than act on stale selection state.
    current=$(promoted_ami)
    if [ "$image_id" = "$current" ]; then
      echo "==> Prune: skipping newly promoted ${image_id}"
      continue
    fi
    matches=$(aws --region "$region" ec2 describe-images --owners self \
      --image-ids "$image_id" \
      --filters "${image_filters[@]}" \
      --query 'length(Images)' --output text)
    if [ "$matches" != 1 ]; then
      echo "Prune: refusing ${image_id}; provenance tags changed after selection" >&2
      return 1
    fi

    echo "==> Prune: deregistering ${image_id}"
    "$repo_root/mise-tasks/packer/deregister-ami.sh" "$image_id" "$region"
  done <<<"$stale"
}

echo "==> qemu-host target: ${architecture} in ${region}"

on_error=cleanup
if [ -t 0 ] && [ -z "${CI:-}" ]; then
  on_error=ask
fi

# An orphaned build instance terminates itself; see shutdown_behavior in
# packer/aws/qemu_host.pkr.hcl.
manifest=$(mktemp)
trap 'rm -f "$manifest"' EXIT
rm -f "$manifest"

packer build \
  -timestamp-ui \
  -warn-on-undeclared-var \
  "--on-error=${on_error}" \
  -only="amazon-ebs.qemu_host" \
  -var "architecture=${architecture}" \
  -var "ubuntu_name=${ubuntu}" \
  -var "qemu_host_build_id=${build_id}" \
  -var "qemu_host_manifest_path=${manifest}" \
  packer/aws

ami=$(python3 -c '
import json, sys
manifest = json.load(open(sys.argv[1]))
print(manifest["builds"][-1]["artifact_id"].split(":")[1])
' "$manifest")
echo "==> Baked ${ami}"

if [ "${usage_promote:-false}" = "true" ]; then
  previous=$(aws --region "$region" ssm get-parameter \
    --name "$param" \
    --query Parameter.Value --output text 2>/dev/null || true)
  aws --region "$region" ssm put-parameter \
    --name "$param" \
    --type String \
    --data-type aws:ec2:image \
    --value "$ami" \
    --overwrite >/dev/null
  echo "==> Promoted ${param} -> ${ami}"
  if [ -n "$previous" ]; then
    echo "    Rollback: aws --region ${region} ssm put-parameter --name ${param} --type String --value ${previous} --overwrite"
  fi
else
  echo "==> Candidate AMI: ${ami}"
  echo "    Promote: aws --region ${region} ssm put-parameter --name ${param} --type String --data-type aws:ec2:image --value ${ami} --overwrite"
fi

prune_old_amis
