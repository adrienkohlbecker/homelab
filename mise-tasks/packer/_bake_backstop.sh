#!/usr/bin/env bash
# Source this from a mise-tasks/packer/ bake wrapper to arm a one-time
# EventBridge Scheduler that terminates the build instance if the job is
# killed before the wrapper's own cleanup runs. A GitLab job-timeout SIGKILL
# tears down the runner's whole process tree, so packer never reaches its
# --on-error=cleanup and the build instance is orphaned (the one failure mode
# that bills forever -- see notes/ci_aws_test_cells.md).
#
# Mirrors the per-cell backstop the test harness arms (test/machine.py
# _arm_backstop): a self-deleting at(<expires>) schedule whose target is
# ec2:terminateInstances via the homelab-ci-cell-scheduler role. Best-effort
# throughout -- a bake must never fail because the backstop could not be armed,
# since the normal on-error cleanup still covers the non-killed path.

# Same role the harness passes; its terminate policy is scoped to role=ci-cell
# and role=ci-ami instances (terraform/aws_ci.tf, ci_cell_scheduler).
BAKE_SCHEDULER_ROLE_ARN="arn:aws:iam::000390721279:role/homelab-ci-cell-scheduler"
# Generous on purpose (matches the harness comment): far longer than any bake,
# short enough that an orphan does not bill for long.
BAKE_BACKSTOP_TTL_HOURS=3

# bake_backstop_arm <region> <build_id> <machine> <ubuntu> <state_file>
#
# Poll (bounded) for the build instance this bake launched -- uniquely
# identified by the packer run tags build_id+machine+ubuntu (the Name tag is
# inconsistent across sources; machine is not) -- then tag it expires_at and
# create the terminate schedule. Writes the schedule name to <state_file>
# *before* the create call, so a kill mid-create still leaves disarm something
# to clean up. Intended to be backgrounded by the caller; returns immediately
# outside CI (locally on-error=ask deliberately keeps a failed build instance
# up for debugging).
bake_backstop_arm() {
  local region="$1" build_id="$2" machine="$3" ubuntu="$4" state_file="$5"
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
  printf '%s\n' "$schedule_name" >"$state_file"

  # Visible expiry tag, matching the cell instances (informational; the
  # schedule is what actually terminates).
  aws --region "$region" ec2 create-tags --resources "$iid" \
    --tags "Key=expires_at,Value=${expires}Z" >/dev/null 2>&1 || true

  # Build the target JSON the same way the harness does: Input is a JSON
  # string nested inside the target JSON -- let python handle the escaping.
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
    : >"$state_file"
  fi
}

# bake_backstop_disarm <region> <state_file> [arm_pid]
#
# Stop the (possibly still-polling) arm process, then delete the schedule it
# created. Runs from the caller's EXIT trap on the normal and error paths; a
# SIGKILL skips it on purpose, leaving the schedule to fire. A fired schedule
# self-deletes (ActionAfterCompletion=DELETE), so a missed disarm is harmless.
bake_backstop_disarm() {
  local region="$1" state_file="$2" pid="${3:-}"
  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  [ -f "$state_file" ] || return 0
  local schedule_name
  schedule_name=$(awk 'NR==1{print}' "$state_file")
  [ -n "$schedule_name" ] || return 0
  aws --region "$region" scheduler delete-schedule --name "$schedule_name" >/dev/null 2>&1 || true
}
