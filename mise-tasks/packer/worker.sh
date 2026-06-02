#!/usr/bin/env bash
#MISE description="Build a Hetzner Cloud CI worker snapshot (noble, no ZFS)"
#USAGE flag "--location <loc>" help="Hetzner datacenter location (comma-separated for fallback, e.g. nbg1,fsn1,hel1)" default="nbg1,fsn1,hel1"
#USAGE flag "--server-type <type>" help="Hetzner server type for the build VM" default="cpx22"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# shellcheck source=_hcloud_token.sh
source "$(dirname "$0")/_hcloud_token.sh"

# Forward MISE_GITHUB_TOKEN if available (raises the GitHub API rate limit
# during mise install inside the build).
mise_token_args=()
if [ -n "${MISE_GITHUB_TOKEN:-}" ]; then
  mise_token_args=(-var "mise_github_token=${MISE_GITHUB_TOKEN}")
fi

# Init the hcloud plugin (pre-installed for the qemu plugins, but the
# hcloud plugin is only declared in hcloud_worker.pkr.hcl).
packer init packer/hcloud_worker.pkr.hcl

# Hetzner capacity is spotty — try each location, but only retry on
# resource_unavailable (not provisioning or other failures).
IFS=',' read -ra locations <<<"$usage_location"
built=false
log=$(mktemp)
for loc in "${locations[@]}"; do
  echo "==> Trying ${usage_server_type} in ${loc}..."
  rc=0
  packer build \
    -timestamp-ui \
    -var "location=${loc}" \
    -var "server_type=${usage_server_type}" \
    "${mise_token_args[@]}" \
    packer/hcloud_worker.pkr.hcl 2>&1 | tee "$log" || rc=$?
  if [ "$rc" -eq 0 ]; then
    built=true
    break
  fi
  if grep -q 'resource_unavailable' "$log"; then
    echo "==> ${loc} unavailable, trying next..."
    continue
  fi
  rm -f "$log"
  exit "$rc"
done
rm -f "$log"

if ! $built; then
  echo "==> All locations exhausted (${usage_location})" >&2
  exit 1
fi

mise run packer:hcloud-prune-snapshots -- "role=ci-worker,ubuntu=noble"

echo "==> Done. Label selector: role=ci-worker,ubuntu=noble"
