#!/usr/bin/env bash
#MISE description="Show a WireGuard client config on demand: terminal QR (phone) or --conf to stdout (laptop). Never written to disk."
#USAGE arg "<device>" help="client peer name (a non-server wireguard_peers entry, e.g. laptop or phone)"
#USAGE flag "--conf" help="emit the raw config to stdout instead of a QR (macOS: pipe to pbcopy, then Add Empty Tunnel -> paste)"
set -euo pipefail

# wg0 client configs are not stored on disk -- they are fully reproducible from
# the vault (private key) + wireguard_psk_seed (PSKs derive via
# filter_plugins/wireguard_psk.py), so we render one on demand and stream it
# straight to a terminal QR (iOS scans it) or stdout (macOS pastes it into Add
# Empty Tunnel). The secret lives only in this pipeline's memory.
#
# wireguard.yml renders the config into a debug msg; ansible.posix.json
# emits the run as JSON so jq can pull just that msg out. ansible prints a
# "Using ... ansible.cfg" banner on stdout ahead of the JSON, so drop everything
# before the first line that is a bare `{`.
device="${usage_device:?usage: mise run wg:show <device> [--conf]}"
conf_mode="${usage_conf:-false}"

config="$(
  ANSIBLE_STDOUT_CALLBACK=ansible.posix.json \
    ansible-playbook wireguard.yml -e "wg_show_peer=${device}" 2>/dev/null |
    sed -n '/^{/,$p' |
    jq -r '.plays[0].tasks[] | select(.task.name=="Render the client config") | .hosts.localhost.msg // empty'
)" || true

if [ -z "$config" ]; then
  echo "wg:show: could not render a config for '${device}' -- is it a client peer (a non-server entry in wireguard_peers)?" >&2
  exit 1
fi

if [ "$conf_mode" = "true" ]; then
  printf '%s\n' "$config"
else
  printf '%s' "$config" | qrencode -t ANSIUTF8
fi
