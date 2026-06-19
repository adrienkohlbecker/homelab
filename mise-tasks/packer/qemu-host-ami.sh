#!/usr/bin/env bash
#MISE description="Bake the AWS nested-qemu runner-host AMI and optionally promote it"
#MISE interactive=true
#USAGE flag "--ubuntu <ubuntu>" help="Ubuntu release codename" default="noble"
#USAGE complete "ubuntu" run="printf 'noble\n'"
#USAGE flag "--promote" help="After a successful bake, write the AMI id to /homelab-ci/ami/qemu-host/<ubuntu>"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# shellcheck source=_bake_backstop.sh
source "$(dirname "$0")/_bake_backstop.sh"

case "${1:-}" in
-h | --help)
  cat <<'EOF'
Usage: mise run packer:qemu-host-ami -- [--ubuntu noble] [--promote]

Bake the AWS nested-qemu runner-host AMI. Without --promote, prints the
candidate AMI and the SSM promotion command.
EOF
  exit 0
  ;;
esac

region=eu-central-1
machine=qemu_host
ubuntu="${usage_ubuntu:-noble}"

case "$ubuntu" in
noble) ;;
*)
  echo "Error: qemu-host AMI currently supports only noble (got '$ubuntu')" >&2
  exit 1
  ;;
esac

artifact_json=$(
  uv run python -c 'import json, platform, yaml; data=yaml.safe_load(open("group_vars/all/versions.yml")); arch = "aarch64" if platform.machine() in {"aarch64", "arm64"} else "x86_64"; print(json.dumps(data["gitlab_runner_archive"][arch]))'
)
runner_url=$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["url"])' "$artifact_json")
runner_sha256=$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["sha256"])' "$artifact_json")

on_error=cleanup
if [ -t 0 ] && [ -z "${CI:-}" ]; then
  on_error=ask
fi

manifest=$(mktemp)
backstop_state=$(mktemp)
backstop_pid=""
trap 'bake_backstop_disarm "$region" "$backstop_state" "$backstop_pid"; rm -rf "$manifest" "$backstop_state"' EXIT
rm -f "$manifest"

bake_backstop_arm "$region" "${CI_PIPELINE_ID:-local}" "$machine" "$ubuntu" "$backstop_state" &
backstop_pid=$!

packer build \
  -timestamp-ui \
  -warn-on-undeclared-var \
  "--on-error=${on_error}" \
  -only="amazon-ebs.qemu_host" \
  -var "ubuntu_name=${ubuntu}" \
  -var "build_id=${CI_PIPELINE_ID:-local}" \
  -var "manifest_path=${manifest}" \
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
