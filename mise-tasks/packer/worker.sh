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

# Init the hcloud plugin (pre-installed for the qemu plugins, but the
# hcloud plugin is only declared in hcloud_worker.pkr.hcl).
packer init packer/hcloud_worker.pkr.hcl

# The ci-worker firewall (terraform/hetzner.tf) is referenced by name
# in hcloud_worker.pkr.hcl's default; no ID lookup needed.
packer build \
  -timestamp-ui \
  -var "location=${usage_location}" \
  -var "server_type=${usage_server_type}" \
  "${mise_token_args[@]}" \
  packer/hcloud_worker.pkr.hcl

mise run packer:hcloud-prune-snapshots "role=ci-worker,ubuntu=noble"

echo "==> Done. Label selector: role=ci-worker,ubuntu=noble"
