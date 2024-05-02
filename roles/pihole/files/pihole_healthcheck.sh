#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euo pipefail

CTR_ID="$(cat /run/pihole.service.ctr-id)"
HEALTH=$(/usr/bin/podman container inspect "$CTR_ID" --format "{{ .State.Healthcheck.Status }}")

if [ "$HEALTH" != "healthy" ]; then
  exit 1
fi
