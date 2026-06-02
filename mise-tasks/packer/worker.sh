#!/usr/bin/env bash
#MISE description="Build a Hetzner Cloud CI worker snapshot (noble, no ZFS)"
#USAGE flag "--location <loc>" help="Hetzner datacenter location" default="nbg1"
#USAGE flag "--server-type <type>" help="Hetzner server type for the build VM" default="cpx22"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# HCLOUD_TOKEN normally arrives as the literal op:// ref (file-based mise tasks
# don't resolve op://, per CLAUDE.md), so re-exec under `op run --` to get the
# real token.
if [[ "${HCLOUD_TOKEN:-}" == op://* ]]; then
	exec op run -- "$0" "$@"
fi

# In CI the resolved token arrives as $HCLOUD_TOKEN_CI (a GitHub Actions secret).
if [ -n "${HCLOUD_TOKEN_CI:-}" ]; then
	HCLOUD_TOKEN="$HCLOUD_TOKEN_CI"
	export HCLOUD_TOKEN
fi

[ -n "${HCLOUD_TOKEN:-}" ] || {
	echo "HCLOUD_TOKEN is unset — sign into 1Password CLI or set it manually." >&2
	exit 1
}

# Forward MISE_GITHUB_TOKEN if available (raises the GitHub API rate limit
# during mise install inside the build).
mise_token_args=()
if [ -n "${MISE_GITHUB_TOKEN:-}" ]; then
	mise_token_args=(-var "mise_github_token=${MISE_GITHUB_TOKEN}")
fi

# Look up the ci-worker firewall ID (terraform/hetzner.tf: SSH from
# home WAN only, default-drop inbound).
FIREWALL_ID=$(curl -fsS -H "Authorization: Bearer ${HCLOUD_TOKEN}" \
	"https://api.hetzner.cloud/v1/firewalls?name=ci-worker" |
	jq '.firewalls[0].id')
[ "$FIREWALL_ID" != "null" ] && [ -n "$FIREWALL_ID" ] || {
	echo "error: could not resolve ci-worker firewall ID — run 'mise run tf apply' first" >&2
	exit 1
}

# Init the hcloud plugin (pre-installed for the qemu plugins, but the
# hcloud plugin is only declared in hcloud_worker.pkr.hcl).
packer init packer/hcloud_worker.pkr.hcl

packer build \
	-timestamp-ui \
	-var "location=${usage_location}" \
	-var "server_type=${usage_server_type}" \
	-var "firewall_ids=[${FIREWALL_ID}]" \
	"${mise_token_args[@]}" \
	packer/hcloud_worker.pkr.hcl

# --- Prune old snapshots ---
# Keep the 2 newest ci-worker snapshots (current + one rollback) plus
# any snapshot a running server was created from. Same policy as the
# fox/hetzner image pruning in mise-tasks/packer/hetzner.sh.
API="https://api.hetzner.cloud/v1"
AUTH=(-H "Authorization: Bearer ${HCLOUD_TOKEN}")
echo "==> Pruning old ci-worker snapshots (keeping newest 2 + any running server's image)"
snaps_json=$(curl -fsS "${AUTH[@]}" "${API}/images?type=snapshot&sort=created:desc&label_selector=role=ci-worker,ubuntu=noble")
servers_json=$(curl -fsS "${AUTH[@]}" "${API}/servers")
# shellcheck disable=SC2086  # word-split the space-separated id list on purpose
stale=$(python3 -c '
import json, sys
snaps = json.loads(sys.argv[1])["images"]
servers = json.loads(sys.argv[2])["servers"]
in_use = {s["image"]["id"] for s in servers if s.get("status") == "running" and s.get("image")}
keep = {i["id"] for i in snaps[:2]} | in_use
print(" ".join(str(i["id"]) for i in snaps if i["id"] not in keep))
' "$snaps_json" "$servers_json")
for old in $stale; do
	echo "    deleting old snapshot $old"
	curl -fsS -X DELETE "${AUTH[@]}" "${API}/images/$old" >/dev/null || true
done

echo "==> Snapshot created. Label selector: role=ci-worker,ubuntu=noble"
echo "    Use 'hcloud image list --selector role=ci-worker' to find it."
