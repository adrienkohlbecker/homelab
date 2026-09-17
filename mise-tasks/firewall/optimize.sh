#!/usr/bin/env bash
#MISE description="Operator diagnostic: run nft --optimize against a host ruleset"
#USAGE arg "<host>" help="inventory host (e.g. lab or pug)"
#USAGE complete "host" run="printf 'lab\npug\n'"
# shellcheck disable=SC2154  # usage_host injected by mise from the #USAGE spec
set -euo pipefail

# This operator-facing task provides a discoverable, read-only entrypoint after
# firewall template edits. `-c` skips the ruleset load; the optimizer prints
# merge suggestions and redundant-rule warnings to stdout.
exec ansible "$usage_host" -b -a "nft -c --optimize -f /etc/nftables.conf"
