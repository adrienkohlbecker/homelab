#!/usr/bin/env bash
#MISE description="Clean up after a cancelled or timed-out qemu-host AMI bake (the CI job's after_script)"
set -euo pipefail

# A cancel or timeout kills Packer before its own cleanup. Find what it created
# for this job by the job_id tag qemu-host-ami.sh passes in its run_tags:
# terminate the build instance and delete its backstop schedule, then delete
# Packer's temporary key pair and security group, which carry the same tag
# whether the job stopped before, during, or after the instance's lifetime. A
# finished bake leaves none of these, so this does nothing.
#
# Packer copies the instance's tags onto the AMI, so a job stopped while the
# AMI was being created also leaves an AMI carrying its job_id. Deregister it
# and delete its snapshots, which AWS allows while both are still pending, but
# only when CI_JOB_STATUS says the job did not succeed: a finished bake's AMI
# carries the same tag. A job can also fail after promoting its AMI, in the
# promotion wait or the pruning that follows, so an AMI that any release's
# qemu-host pointer names is kept, and so is every AMI when the pointers
# cannot be read.
region=eu-central-1
job_filter="Name=tag:job_id,Values=${CI_JOB_ID:?CI_JOB_ID is required}"

# shutting-down too: Packer may have been killed while terminating it.
instances=$(aws ec2 describe-instances --region "$region" \
  --filters "$job_filter" "Name=instance-state-name,Values=pending,running,shutting-down,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
for id in $instances; do
  echo "terminating leftover build instance ${id}"
  aws ec2 terminate-instances --region "$region" --instance-ids "$id" >/dev/null
  # The schedule only exists when the backstop armed.
  aws scheduler delete-schedule --region "$region" --name "ci-bake-${id}" 2>/dev/null || true
  # A security group stays in use until its instance is gone.
  aws ec2 wait instance-terminated --region "$region" --instance-ids "$id"
done

key_names=$(aws ec2 describe-key-pairs --region "$region" --filters "$job_filter" \
  --query 'KeyPairs[].KeyName' --output text)
for key in $key_names; do
  echo "deleting key pair ${key}"
  aws ec2 delete-key-pair --region "$region" --key-name "$key"
done

group_ids=$(aws ec2 describe-security-groups --region "$region" --filters "$job_filter" \
  --query 'SecurityGroups[].GroupId' --output text)
for group in $group_ids; do
  echo "deleting security group ${group}"
  aws ec2 delete-security-group --region "$region" --group-id "$group"
done

if [ "${CI_JOB_STATUS:-success}" != success ]; then
  repo_root=$(git rev-parse --show-toplevel)
  templates=$(yq -r '.[].ci.ami_parameter' "${repo_root}/data/architectures.yml")
  releases=$(yq -r '.releases | keys | .[]' "${repo_root}/data/ubuntu_releases.yml")
  pointers=()
  for template in $templates; do
    for release in $releases; do
      pointers+=("${template//\{ubuntu\}/$release}")
    done
  done

  images=$(aws ec2 describe-images --region "$region" --owners self \
    --filters "$job_filter" \
    --query "Images[].[ImageId,join(',',BlockDeviceMappings[].Ebs.SnapshotId)]" \
    --output text)
  while read -r image snapshots; do
    [ -n "$image" ] || continue
    references=$(aws ssm get-parameters --region "$region" --names "${pointers[@]}" \
      --query "length(Parameters[?Value=='${image}'])" --output text) || {
      echo "cannot read the qemu-host AMI pointers; keeping ${image}" >&2
      exit 1
    }
    if [ "$references" != 0 ]; then
      echo "keeping promoted AMI ${image}"
      continue
    fi
    echo "deregistering unfinished AMI ${image}"
    aws ec2 deregister-image --region "$region" --image-id "$image"
    for snapshot in ${snapshots//,/ }; do
      echo "deleting snapshot ${snapshot}"
      aws ec2 delete-snapshot --region "$region" --snapshot-id "$snapshot"
    done
  done <<<"$images"
fi
