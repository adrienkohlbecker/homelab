#!/usr/bin/env bash
#MISE description="Prune old Hetzner Cloud snapshots, keeping newest 2 + any running server's image"
#USAGE arg "<selector>" help="Label selector for snapshots to prune (e.g. 'os=ubuntu-zfs,ubuntu=jammy')"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# hcloud authenticates on its own: $HCLOUD_TOKEN in CI, or the local CLI context
# (~/.config/hcloud/cli.toml) on the workstation.
SELECTOR="$usage_selector"

echo "==> Pruning snapshots matching '${SELECTOR}' (keeping newest 2 + running servers)"
snaps_json=$(hcloud image list --type snapshot --selector "$SELECTOR" --sort created:desc -o json)
servers_json=$(hcloud server list -o json)
stale=$(python3 -c '
import json, sys
snaps = json.loads(sys.argv[1])
servers = json.loads(sys.argv[2])
in_use = {s["image"]["id"] for s in servers if s.get("status") == "running" and s.get("image")}
keep = {i["id"] for i in snaps[:2]} | in_use
print(" ".join(str(i["id"]) for i in snaps if i["id"] not in keep))
' "$snaps_json" "$servers_json")
count=0
# shellcheck disable=SC2086  # word-split the space-separated id list on purpose
for old in $stale; do
  echo "    deleting snapshot $old"
  hcloud image delete "$old" >/dev/null ||
    echo "    WARNING: failed to delete snapshot $old" >&2
  count=$((count + 1))
done
if [ "$count" -eq 0 ]; then
  echo "    nothing to prune"
fi
