#!/usr/bin/env bash
#MISE description="Deregister an AMI and delete its unshared backing snapshots"
#USAGE arg "<ami>" help="AMI id"
#USAGE arg "[region]" help="AWS region" default="eu-central-1"
set -euo pipefail

ami="${usage_ami:-${1:-}}"
region="${usage_region:-${2:-eu-central-1}}"

if [ -z "$ami" ]; then
  echo "usage: deregister-ami.sh <ami-id> [region]" >&2
  exit 1
fi

if ! [[ $ami =~ ^ami-[0-9a-f]+$ ]]; then
  echo "deregister-ami.sh: invalid AMI id '$ami'" >&2
  exit 1
fi

failures=$(aws --region "$region" ec2 deregister-image \
  --image-id "$ami" \
  --delete-associated-snapshots \
  --query "DeleteSnapshotResults[?ReturnCode!='success']" \
  --output json)

if [ "$failures" != "[]" ]; then
  echo "deregister-ami.sh: $ami deregistered but snapshot deletion was incomplete:" >&2
  echo "$failures" >&2
  exit 1
fi

echo "Deregistered $ami and deleted its unshared backing snapshots"
