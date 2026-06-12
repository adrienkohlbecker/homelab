#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

CTR_ID="$(cat /run/adguard.service.ctr-id)"
HEALTH=$(/usr/bin/podman container inspect "$CTR_ID" --format "{{ .State.Healthcheck.Status }}")

if [ "$HEALTH" != "healthy" ]; then
  echo >&2 "Error: adguard container health=$HEALTH (not healthy)"
  exit 1
fi
