#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

# Path mirrors pihole.service's --cidfile=%t/%n.ctr-id (%t=/run,
# %n=pihole.service). A missing file makes this read fail under the strict
# mode inherited from functions.sh -> non-zero exit -> keepalived enters
# FAULT, which is the intended fail-safe (no container = unhealthy).
CTR_ID="$(cat /run/pihole.service.ctr-id)"
HEALTH=$(/usr/bin/podman container inspect "$CTR_ID" --format "{{ .State.Healthcheck.Status }}")

if [ "$HEALTH" != "healthy" ]; then
  # Surfaces in `journalctl -t Keepalived_vrrp` when the VIP enters FAULT.
  echo >&2 "Error: pihole container health=$HEALTH (not healthy)"
  exit 1
fi
