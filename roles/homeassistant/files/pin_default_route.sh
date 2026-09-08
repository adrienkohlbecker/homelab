#!/usr/bin/env bash
# Down-rank HomeAssistant's mac_iot default so a podman bridge wins when both
# exist, but preserve it when it is the only route. Runs host-side because HA
# has no CAP_NET_ADMIN. Best-effort: a route hiccup must not block HA startup.
set -euo pipefail

read -r pid gw < <(
  /usr/bin/podman inspect \
    -f '{{.State.Pid}} {{.NetworkSettings.Networks.mac_iot.Gateway}}' \
    homeassistant 2>/dev/null
) || exit 0
[[ -n "$pid" && -n "$gw" ]] || exit 0

# Only touch the macvlan default when a second default exists to fall back
# on -- otherwise removing it would cut HA's only route off-subnet.
defaults="$(nsenter -t "$pid" -n ip -4 route show default 2>/dev/null | wc -l || true)"
[[ "${defaults:-0}" -gt 1 ]] || exit 0

# Re-add at a high metric (lower priority) rather than dropping outright,
# so the macvlan stays a fallback if the bridge default ever disappears.
nsenter -t "$pid" -n ip route del default via "$gw" 2>/dev/null || true
nsenter -t "$pid" -n ip route add default via "$gw" metric 1000 2>/dev/null || true
exit 0
