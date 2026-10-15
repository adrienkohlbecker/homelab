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
# with its snapshots, which AWS allows while both are still pending, but only
# when CI_JOB_STATUS says the job did not succeed and the AMI lacks the
# bake_complete tag qemu-host-ami.sh sets before promoting: a job can still
# fail or be cancelled while SSM publishes the promotion, or in the pruning
# that follows. An AMI that any release's qemu-host pointer names is kept too,
# and so is every AMI when the pointers cannot be read.
region=eu-central-1
job_filter="Name=tag:job_id,Values=${CI_JOB_ID:?CI_JOB_ID is required}"
# What could not be removed. The rest of the cleanup still runs, then the job
# fails naming it.
leftovers=()

# EC2 refuses a group with DependencyViolation until the terminated instance's
# network interface is released, which can lag the termination waiter. Retry
# that for up to two minutes; any other error fails at once.
delete_security_group() {
  local error
  for _ in $(seq 1 12); do
    error=$(aws ec2 delete-security-group --region "$region" --group-id "$1" 2>&1 >/dev/null) && return 0
    [[ $error == *DependencyViolation* ]] || break
    sleep 10
  done
  echo "$error" >&2
  return 1
}

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
  delete_security_group "$group" || leftovers+=("security group ${group}")
done

clean_unfinished_amis() {
  local images promoted image
  images=$(aws ec2 describe-images --region "$region" --owners self \
    --filters "$job_filter" \
    --query "Images[?!(Tags[?Key=='bake_complete'])].ImageId" --output text)
  [ -n "$images" ] || return 0
  promoted=$(aws ssm get-parameters-by-path --region "$region" --path /homelab-ci/ami/qemu-host --recursive \
    --query 'Parameters[].Value' --output text) || {
    echo "cannot read the qemu-host AMI pointers; keeping every AMI of job ${CI_JOB_ID}" >&2
    leftovers+=("unchecked AMIs ${images}")
    return 0
  }
  for image in $images; do
    if grep -qwF "$image" <<<"$promoted"; then
      echo "keeping promoted AMI ${image}"
      continue
    fi
    echo "deregistering unfinished AMI ${image}"
    "$(dirname "${BASH_SOURCE[0]}")/deregister-ami.sh" "$image" "$region" || leftovers+=("AMI ${image}")
  done
}
if [ "${CI_JOB_STATUS:-success}" != success ]; then
  clean_unfinished_amis
fi

if [ "${#leftovers[@]}" -gt 0 ]; then
  printf 'left behind by job %s: %s\n' "$CI_JOB_ID" "${leftovers[@]}" >&2
  exit 1
fi
