#!/usr/bin/env bash
#MISE description="Run nft --optimize against /etc/nftables.conf on a host and print suggestions"
#MISE alias="firewall:optimize"
#USAGE arg "<host>" help="inventory host (e.g. lab, pug, box)"
# shellcheck disable=SC2154  # usage_host injected by mise from the #USAGE spec
set -euo pipefail

# --optimize is read-only: `-c` skips the actual ruleset load and the
# optimizer prints set/vmap merge suggestions + redundant-rule
# warnings to stdout. Use this after a non-trivial template edit (or
# periodically) to see if the assembled ruleset has slack — e.g.
# multiple rules that could collapse into one vmap, or interval sets
# that overlap.
exec ansible -i hosts.ini "$usage_host" -b -m command -a "nft -c --optimize -f /etc/nftables.conf"
