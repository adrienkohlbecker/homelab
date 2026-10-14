#!/usr/bin/env bash
#MISE description="Clean up after a cancelled or timed-out qemu-host AMI bake (the CI job's after_script)"
set -euo pipefail

# A cancel or timeout kills Packer before its own cleanup. Find this job's
# build instance by the job_id tag qemu-host-ami.sh gives it, terminate it,
# and delete what was created for it: its backstop schedule and Packer's
# temporary key pair and security group. A finished bake leaves no instance,
# so this does nothing.
region=eu-central-1
job_id=${CI_JOB_ID:?CI_JOB_ID is required}

instances=$(aws ec2 describe-instances --region "$region" \
  --filters "Name=tag:job_id,Values=${job_id}" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query "Reservations[].Instances[].[InstanceId,KeyName,join(',',SecurityGroups[].GroupId)]" \
  --output text)

while read -r id key groups; do
  [ -n "$id" ] || continue
  echo "terminating leftover build instance ${id}"
  aws ec2 terminate-instances --region "$region" --instance-ids "$id" >/dev/null
  # The schedule only exists when the backstop armed.
  aws scheduler delete-schedule --region "$region" --name "ci-bake-${id}" 2>/dev/null || true

  # A security group stays in use until its instance is gone.
  aws ec2 wait instance-terminated --region "$region" --instance-ids "$id"
  if [[ $key == packer_* ]]; then
    echo "deleting key pair ${key}"
    aws ec2 delete-key-pair --region "$region" --key-name "$key"
  fi
  for group in ${groups//,/ }; do
    name=$(aws ec2 describe-security-groups --region "$region" --group-ids "$group" \
      --query 'SecurityGroups[0].GroupName' --output text)
    if [[ $name == packer_* ]]; then
      echo "deleting security group ${group} (${name})"
      aws ec2 delete-security-group --region "$region" --group-id "$group"
    fi
  done
done <<<"$instances"
