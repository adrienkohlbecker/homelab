#!/usr/bin/env bash
# Down-rank HomeAssistant's mac_iot (macvlan) default route so a podman
# bridge default wins where both exist (netavark installs a default per
# gateway-bearing network at equal metric -- an unpinned tie,
# the shape lab is in), while leaving it as-is where the macvlan is the
# only default (single-default hosts) so HA's egress is never stranded.
# Runs on the host (HA has no CAP_NET_ADMIN) and edits inside the
# container netns via nsenter. Invoked from homeassistant.service.j2
# ExecStartPost with the mac_iot gateway as $1. Best-effort: never fails
# the unit -- a route hiccup must not block HA from starting.
set -euo pipefail

gw="${1:-}"
[[ -n "$gw" ]] || exit 0

pid="$(/usr/bin/podman inspect -f '{{.State.Pid}}' homeassistant 2>/dev/null)" || exit 0
[[ -n "$pid" ]] || exit 0

# Only touch the macvlan default when a second default exists to fall back
# on -- otherwise removing it would cut HA's only route off-subnet.
defaults="$(nsenter -t "$pid" -n ip -4 route show default 2>/dev/null | wc -l || true)"
[[ "${defaults:-0}" -gt 1 ]] || exit 0

# Re-add at a high metric (lower priority) rather than dropping outright,
# so the macvlan stays a fallback if the bridge default ever disappears.
nsenter -t "$pid" -n ip route del default via "$gw" 2>/dev/null || true
nsenter -t "$pid" -n ip route add default via "$gw" metric 1000 2>/dev/null || true
exit 0
