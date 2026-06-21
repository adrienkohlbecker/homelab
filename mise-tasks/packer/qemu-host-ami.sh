#!/usr/bin/env bash
#MISE description="Bake the AWS nested-qemu runner-host AMI and optionally promote it"
#MISE interactive=true
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="noble"
#USAGE complete "ubuntu" run="printf 'noble\n'"
#USAGE flag "--promote" help="After a successful bake, write the AMI id to /homelab-ci/ami/qemu-host/<ubuntu>"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

BAKE_SCHEDULER_ROLE_ARN="arn:aws:iam::000390721279:role/homelab-ci-cell-scheduler"
BAKE_BACKSTOP_TTL_HOURS=3
region=eu-central-1
machine=qemu_host
ubuntu="${usage_ubuntu:-noble}"
build_id="${CI_PIPELINE_ID:-local}"

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
      "Name=instance-state-name,Values=pending,running" \
      --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null | awk '{print $1; exit}')
    [ -n "$iid" ] && [ "$iid" != "None" ] && break
    iid=""
    sleep 5
    waited=$((waited + 5))
  done
  if [ -z "$iid" ]; then
    echo "bake-backstop: no build instance found for ${machine}/${ubuntu} in ${build_id}; not armed" >&2
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

case "$ubuntu" in
noble) ;;
*)
  echo "Error: qemu-host AMI currently supports only noble (got '$ubuntu')" >&2
  exit 1
  ;;
esac

read -r runner_url runner_sha256 < <(
  uv run python - <<'PY'
import yaml

with open("group_vars/all/versions.yml") as fh:
    artifact = yaml.safe_load(fh)["gitlab_runner_archive"]["x86_64"]
print(artifact["url"], artifact["sha256"])
PY
)
echo "==> qemu-host target architecture: x86_64"
echo "==> gitlab-runner binary: ${runner_url}"

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
  -var "ubuntu_name=${ubuntu}" \
  -var "qemu_host_build_id=${build_id}" \
  -var "qemu_host_manifest_path=${manifest}" \
  -var "gitlab_runner_url=${runner_url}" \
  -var "gitlab_runner_sha256=${runner_sha256}" \
  packer/aws

ami=$(python3 -c '
import json, sys
manifest = json.load(open(sys.argv[1]))
print(manifest["builds"][-1]["artifact_id"].split(":")[1])
' "$manifest")
echo "==> Baked ${ami}"

param="/homelab-ci/ami/qemu-host/${ubuntu}"
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
