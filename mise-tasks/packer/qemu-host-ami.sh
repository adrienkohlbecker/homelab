#!/usr/bin/env bash
#MISE description="Bake the AWS nested-qemu runner-host AMI and optionally promote it"
#MISE interactive=true
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="noble"
#USAGE complete "ubuntu" run="yq -r '.releases | keys | .[]' data/ubuntu_releases.yml"
#USAGE flag "--architecture <architecture>" help="Target architecture (x86_64 or aarch64)" default="x86_64"
#USAGE flag "--region <region>" help="AWS region" default="eu-central-1"
#USAGE flag "--promote" help="After a successful bake, update the architecture-specific qemu-host AMI pointer"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

BAKE_SCHEDULER_ROLE_ARN="arn:aws:iam::000390721279:role/homelab-ci-cell-scheduler"
BAKE_BACKSTOP_TTL_HOURS=3
machine=qemu_host
ubuntu="${usage_ubuntu:-noble}"
architecture="${usage_architecture:-x86_64}"
build_id="${CI_PIPELINE_ID:-local}"
repo_root=$(git rev-parse --show-toplevel)

case "$architecture" in
x86_64)
  name_prefix=homelab-ci-qemu-host
  param="/homelab-ci/ami/qemu-host/${ubuntu}"
  ;;
aarch64)
  name_prefix=homelab-ci-qemu-host-aarch64
  param="/homelab-ci/ami/qemu-host/aarch64/${ubuntu}"
  ;;
*)
  echo "qemu-host-ami: unsupported architecture ${architecture}" >&2
  exit 2
  ;;
esac
region="${usage_region:-eu-central-1}"

# CI job timeouts can skip packer's cleanup. Arm a self-deleting terminate
# schedule for the build instance, then disarm it on normal exit.
bake_backstop_arm() {
  [ -n "${CI:-}" ] || return 0

  local iid="" waited=0
  while [ "$waited" -lt 180 ]; do
    iid=$(aws --region "$region" ec2 describe-instances \
      --filters "Name=tag:build_id,Values=${build_id}" \
      "Name=tag:machine,Values=${machine}" \
      "Name=tag:ubuntu,Values=${ubuntu}" \
      "Name=tag:architecture,Values=${architecture}" \
      "Name=instance-state-name,Values=pending,running" \
      --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null | awk '{print $1; exit}')
    [ -n "$iid" ] && [ "$iid" != "None" ] && break
    iid=""
    sleep 5
    waited=$((waited + 5))
  done
  if [ -z "$iid" ]; then
    echo "bake-backstop: no build instance found for ${machine}/${architecture}/${ubuntu} in ${build_id}; not armed" >&2
    return 0
  fi

  local expires schedule_name target
  expires=$(date -u -d "+${BAKE_BACKSTOP_TTL_HOURS} hours" +%Y-%m-%dT%H:%M:%S 2>/dev/null ||
    date -u -v "+${BAKE_BACKSTOP_TTL_HOURS}H" +%Y-%m-%dT%H:%M:%S)
  schedule_name="ci-bake-${iid}"
  printf '%s\n' "$schedule_name" >"$backstop_state"

  aws --region "$region" ec2 create-tags --resources "$iid" \
    --tags "Key=expires_at,Value=${expires}Z" >/dev/null 2>&1 || true

  target=$(python3 -c 'import json, sys
print(json.dumps({
    "Arn": "arn:aws:scheduler:::aws-sdk:ec2:terminateInstances",
    "RoleArn": sys.argv[1],
    "Input": json.dumps({"InstanceIds": [sys.argv[2]]}),
}))' "$BAKE_SCHEDULER_ROLE_ARN" "$iid")

  if aws --region "$region" scheduler create-schedule \
    --name "$schedule_name" \
    --schedule-expression "at(${expires})" \
    --schedule-expression-timezone UTC \
    --flexible-time-window Mode=OFF \
    --action-after-completion DELETE \
    --target "$target" >/dev/null 2>&1; then
    echo "bake-backstop: armed ${schedule_name} (terminates ${iid} at ${expires}Z)" >&2
  else
    echo "bake-backstop: could not create ${schedule_name} (IAM not applied?); not armed" >&2
    : >"$backstop_state"
  fi
}

bake_backstop_disarm() {
  if [ -n "$backstop_pid" ]; then
    kill "$backstop_pid" 2>/dev/null || true
    wait "$backstop_pid" 2>/dev/null || true
  fi
  [ -f "$backstop_state" ] || return 0
  local schedule_name
  schedule_name=$(awk 'NR==1{print}' "$backstop_state")
  [ -n "$schedule_name" ] || return 0
  aws --region "$region" scheduler delete-schedule --name "$schedule_name" >/dev/null 2>&1 || true
}

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

manifest=$(mktemp)
backstop_state=$(mktemp)
backstop_pid=""
trap 'bake_backstop_disarm; rm -f "$manifest" "$backstop_state"' EXIT
rm -f "$manifest"

bake_backstop_arm &
backstop_pid=$!

packer build \
  -timestamp-ui \
  -warn-on-undeclared-var \
  "--on-error=${on_error}" \
  -only="amazon-ebs.qemu_host" \
  -var "aws_region=${region}" \
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
