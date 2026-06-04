#!/usr/bin/env bash
#MISE description="Show a WireGuard client config on demand: terminal QR (phone) or --conf to stdout (laptop). Never written to disk."
#USAGE arg "<device>" help="client peer name (a non-server wireguard_peers entry, e.g. laptop or phone)"
#USAGE complete "device" run="grep '^\\s*- name:' group_vars/all/main.yml | awk '{print $3}'"
#USAGE flag "--conf" help="emit the raw config to stdout instead of a QR (macOS: pipe to pbcopy, then Add Empty Tunnel -> paste)"
set -euo pipefail

# wg0 client configs are not stored on disk -- they are fully reproducible from
# the vault (private key) + wireguard_psk_seed (PSKs derive via
# filter_plugins/wireguard_psk.py), so we render one on demand and stream it
# straight to a terminal QR (iOS scans it) or stdout (macOS pastes it into Add
# Empty Tunnel). The secret lives only in this pipeline's memory.
#
# wireguard.yml renders the config into a debug msg; ansible.posix.json emits the
# run as JSON so jq can pull that msg out. Keep ansible's stderr (don't redirect
# it to /dev/null) so a real failure -- a locked vault, a missing key -- surfaces
# instead of being swallowed into the generic "could not render" message below.
device="${usage_device:?usage: mise run wg:show <device> [--conf]}"
conf_mode="${usage_conf:-false}"

err="$(mktemp)"
trap 'rm -f "$err"' EXIT

json="$(
  ANSIBLE_STDOUT_CALLBACK=ansible.posix.json \
    ansible-playbook wireguard.yml -e "wg_show_peer=${device}" 2>"$err"
)" || true

# ansible prints a "Using ... ansible.cfg" banner on stdout ahead of the JSON, so
# drop everything before the first bare `{`, then pull the rendered config out by
# task NAME -- not by play/task index (.plays[0].tasks[N]), which adding a task
# would silently break.
strip_banner() { sed -n '/^{/,$p'; }
config="$(printf '%s\n' "$json" | strip_banner |
  jq -r '.plays[].tasks[]? | select(.task.name == "Render the client config") | .hosts[].msg // empty' 2>/dev/null || true)"

if [ -z "$config" ]; then
  {
    echo "wg:show: could not render a config for '${device}'."
    echo "It must be a client peer (a non-server entry in wireguard_peers), and the vault must be unlocked."
    # Surface the real cause instead of swallowing it: any failed-task message
    # from the run (e.g. the playbook's known-peer assert) + ansible's stderr.
    printf '%s\n' "$json" | strip_banner |
      jq -r '.. | objects | select(.failed? == true) | .msg? // empty' 2>/dev/null || true
    cat "$err"
  } >&2
  exit 1
fi

if [ "$conf_mode" = "true" ]; then
  printf '%s\n' "$config"
else
  printf '%s' "$config" | qrencode -t ANSIUTF8
fi
