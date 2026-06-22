#!/usr/bin/env bash
#MISE description="Show a WireGuard client config on demand: terminal QR (phone) or --conf to stdout (laptop). Never written to disk."
#USAGE arg "<device>" help="client peer name (a non-server wireguard_peers entry, e.g. laptop or phone)"
#USAGE complete "device" run="yq -r '.wireguard_peers[] | select(.is_server != true) | .name' group_vars/all/main.yml"
#USAGE flag "--conf" help="emit the raw config to stdout instead of a QR (macOS: pipe to pbcopy, then Add Empty Tunnel -> paste)"
set -euo pipefail

# Client configs are reproducible from vaulted peer keys plus the PSK seed, so
# this task renders one on demand and streams it to QR/stdout without writing it.
device="${usage_device:?usage: mise run wg:show <device> [--conf]}"

err="$(mktemp)"
trap 'rm -f "$err"' EXIT

json="$(
  ANSIBLE_STDOUT_CALLBACK=ansible.posix.json \
    ansible-playbook wireguard.yml -e "wg_show_peer=${device}" 2>"$err"
)" || true

# ansible prints a "Using ... ansible.cfg" banner on stdout ahead of the JSON, so
# drop everything before the first bare `{`.
payload="$(sed -n '/^{/,$p' <<<"$json")"
config="$(jq -r '.plays[].tasks[]? | select(.task.name == "Render the client config") | .hosts[].msg // empty' <<<"$payload" 2>/dev/null || true)"

if [ -z "$config" ]; then
  {
    echo "wg:show: could not render a config for '${device}'."
    echo "It must be a client peer (a non-server entry in wireguard_peers), and the vault must be unlocked."
    jq -r '.. | objects | select(.failed? == true) | .msg? // empty' <<<"$payload" 2>/dev/null || true
    cat "$err"
  } >&2
  exit 1
fi

if [ "${usage_conf:-false}" = "true" ]; then
  printf '%s\n' "$config"
else
  printf '%s' "$config" | qrencode -t ANSIUTF8
fi
