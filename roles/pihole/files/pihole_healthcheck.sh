#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

CTR_ID="$(cat /run/pihole.service.ctr-id)"
HEALTH=$(/usr/bin/podman container inspect "$CTR_ID" --format "{{ .State.Healthcheck.Status }}")

if [ "$HEALTH" != "healthy" ]; then
  exit 1
fi
