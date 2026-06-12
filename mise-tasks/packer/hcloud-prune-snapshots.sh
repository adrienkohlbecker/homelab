#!/usr/bin/env bash
#MISE description="Prune old Hetzner Cloud snapshots, keeping newest 2 + any running server's image"
#USAGE arg "<selector>" help="Label selector for snapshots to prune (e.g. 'os=ubuntu-zfs,ubuntu=jammy')"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# The caller (packer:hetzner) resolves the token before invoking this
# script; it must already be a real token, not an op:// ref.
[ -n "${HCLOUD_TOKEN:-}" ] || {
  echo "HCLOUD_TOKEN is unset" >&2
  exit 1
}

API="https://api.hetzner.cloud/v1"
AUTH=(-H "Authorization: Bearer ${HCLOUD_TOKEN}")
SELECTOR="$usage_selector"
SELECTOR_ENC=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$SELECTOR")

echo "==> Pruning snapshots matching '${SELECTOR}' (keeping newest 2 + running servers)"
snaps_json=$(curl -fsS --retry 3 --retry-all-errors "${AUTH[@]}" "${API}/images?type=snapshot&sort=created:desc&per_page=50&label_selector=${SELECTOR_ENC}")
servers_json=$(curl -fsS --retry 3 --retry-all-errors "${AUTH[@]}" "${API}/servers?per_page=50")
# shellcheck disable=SC2086  # word-split the space-separated id list on purpose
stale=$(python3 -c '
import json, sys
snaps = json.loads(sys.argv[1])["images"]
servers = json.loads(sys.argv[2])["servers"]
in_use = {s["image"]["id"] for s in servers if s.get("status") == "running" and s.get("image")}
keep = {i["id"] for i in snaps[:2]} | in_use
print(" ".join(str(i["id"]) for i in snaps if i["id"] not in keep))
' "$snaps_json" "$servers_json")
count=0
for old in $stale; do
  echo "    deleting snapshot $old"
  curl -fsS --retry 3 --retry-all-errors -X DELETE "${AUTH[@]}" "${API}/images/$old" >/dev/null ||
    echo "    WARNING: failed to delete snapshot $old" >&2
  count=$((count + 1))
done
if [ "$count" -eq 0 ]; then
  echo "    nothing to prune"
fi
