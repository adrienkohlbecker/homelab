#!/usr/bin/env bash
#MISE description="Build a Hetzner Cloud CI worker snapshot (noble, no ZFS)"
#USAGE flag "--location <loc>" help="Hetzner datacenter location (comma-separated for fallback, e.g. nbg1,fsn1,hel1)" default="nbg1,fsn1,hel1"
#USAGE flag "--server-type <type>" help="Hetzner server type for the build VM" default="cpx22"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# shellcheck source=_hcloud_token.sh
source "$(dirname "$0")/_hcloud_token.sh"

# Forward MISE_GITHUB_TOKEN if available (raises the GitHub API rate limit
# during mise install inside the build). Passed via env (PKR_VAR_*) rather
# than -var to keep the token out of /proc/*/cmdline.
if [ -n "${MISE_GITHUB_TOKEN:-}" ]; then
  export PKR_VAR_mise_github_token="$MISE_GITHUB_TOKEN"
fi

# --on-error=ask keeps the failed build VM up for SSH debugging, but only
# with a human at the terminal. CI has no stdin — fall back to cleanup.
on_error=cleanup
if [ -t 0 ] && [ -z "${CI:-}" ]; then
  on_error=ask
fi

# Init the hcloud plugin (pre-installed for the qemu plugins, but the
# hcloud plugin is only declared in hcloud_worker.pkr.hcl).
packer init packer/hcloud_worker.pkr.hcl

# Hetzner capacity is spotty — try each location, but only retry on
# resource_unavailable (not provisioning or other failures).
IFS=',' read -ra locations <<<"$usage_location"
built=false
log=$(mktemp)
trap 'rm -f "$log"' EXIT
for loc in "${locations[@]}"; do
  echo "==> Trying ${usage_server_type} in ${loc}..."
  rc=0
  packer build \
    -timestamp-ui \
    -warn-on-undeclared-var \
    "--on-error=${on_error}" \
    -var "location=${loc}" \
    -var "server_type=${usage_server_type}" \
    packer/hcloud_worker.pkr.hcl 2>&1 | tee "$log" || rc=$?
  if [ "$rc" -eq 0 ]; then
    built=true
    break
  fi
  if grep -q 'resource_unavailable' "$log"; then
    echo "==> ${loc} unavailable, trying next..."
    continue
  fi
  exit "$rc"
done

if ! $built; then
  echo "==> All locations exhausted (${usage_location})" >&2
  exit 1
fi

mise run packer:hcloud-prune-snapshots -- "role=ci-worker,ubuntu=noble"

echo "==> Done. Label selector: role=ci-worker,ubuntu=noble"
