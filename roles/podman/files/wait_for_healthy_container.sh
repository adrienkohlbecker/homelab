#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

f_require_root

CIDFILE="${1:-}"
if [ -z "$CIDFILE" ]; then
  f_fail "Usage: $F_SCRIPT CIDFILE"
fi

if [ ! -f "$CIDFILE" ]; then
  f_fail "Error: CIDFILE '$CIDFILE' does not exist"
fi

sleep 1
while [ "$(/usr/bin/podman container inspect "$(cat "$CIDFILE")" --format "{{.State.Healthcheck.Status}}")" != "healthy" ]; do
  sleep 1
done
