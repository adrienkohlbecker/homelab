#!/usr/bin/env bash
#MISE description="Operator diagnostic: run nft --optimize against a host ruleset"
#USAGE arg "<host>" help="inventory host (e.g. lab, pug, box)"
#USAGE complete "host" run="awk '/^\\[/{next} /^$/{next} {print $1}' hosts.ini | sort -u"
# shellcheck disable=SC2154  # usage_host injected by mise from the #USAGE spec
set -euo pipefail

# This operator-facing task provides a discoverable, read-only entrypoint after
# firewall template edits. `-c` skips the ruleset load; the optimizer prints
# merge suggestions and redundant-rule warnings to stdout.
exec ansible -i hosts.ini "$usage_host" -b -m command -a "nft -c --optimize -f /etc/nftables.conf"
