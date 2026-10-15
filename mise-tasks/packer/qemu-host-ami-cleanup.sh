#!/usr/bin/env bash
#MISE description="Clean up after a cancelled or timed-out qemu-host AMI bake (the CI job's after_script)"
set -euo pipefail

# A cancel or timeout kills Packer before its own cleanup. Find this job's
# build instance by the job_id tag qemu-host-ami.sh gives it, terminate it,
# and delete what was created for it: its backstop schedule and Packer's
# temporary key pair and security group. A finished bake leaves no instance,
# so this does nothing.
#
# Packer copies the instance's tags onto the AMI, so a job stopped while the
# AMI was being created also leaves an AMI carrying its job_id. Deregister it
# and delete its snapshots, which AWS allows while both are still pending, but
# only when CI_JOB_STATUS says the job did not succeed: a finished bake's AMI
# carries the same tag.
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

if [ "${CI_JOB_STATUS:-success}" != success ]; then
  images=$(aws ec2 describe-images --region "$region" --owners self \
    --filters "Name=tag:job_id,Values=${job_id}" \
    --query "Images[].[ImageId,join(',',BlockDeviceMappings[].Ebs.SnapshotId)]" \
    --output text)
  while read -r image snapshots; do
    [ -n "$image" ] || continue
    echo "deregistering unfinished AMI ${image}"
    aws ec2 deregister-image --region "$region" --image-id "$image"
    for snapshot in ${snapshots//,/ }; do
      echo "deleting snapshot ${snapshot}"
      aws ec2 delete-snapshot --region "$region" --snapshot-id "$snapshot"
    done
  done <<<"$images"
fi
