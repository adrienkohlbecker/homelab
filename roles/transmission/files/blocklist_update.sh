#!/bin/bash
# Weekly blocklist refresh (transmission only fetches blocklist-url on an
# explicit --blocklist-update, never on a schedule or at startup). Driven by
# the transmission_blocklist_update systemd timer. RPC auth is enabled, so the
# call authenticates; the inner `sh -c` runs inside the container, reading the
# rpc-password from the mounted secret. The credential is kept off this host's
# process argv and out of the unit file -- it does land on the in-container
# transmission-remote argv, which is acceptable since anything that can read
# that argv can already read the mounted secret. 127.0.0.1 is always allowed
# past the rpc-host-whitelist.
set -euo pipefail

# shellcheck disable=SC2016  # $(...) is deliberately expanded inside the container, not here.
exec podman exec transmission sh -c \
  'transmission-remote --auth "transmission:$(cat /run/secrets/pass)" 127.0.0.1:9091 --blocklist-update'
