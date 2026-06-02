#!/usr/bin/env bash
# Source this from any mise-tasks/packer/ script that needs HCLOUD_TOKEN.
#
# Two resolution paths:
# 1. HCLOUD_TOKEN already set — CI (GitHub Actions secret) or manual export.
# 2. HCLOUD_TOKEN_OP — op:// ref from mise.toml [env]; re-exec under
#    `op run --` to resolve it (local workstation).

if [ -z "${HCLOUD_TOKEN:-}" ] && [[ "${HCLOUD_TOKEN_OP:-}" == op://* ]]; then
  export HCLOUD_TOKEN_OP
  exec op run -- "$0" "$@"
elif [ -z "${HCLOUD_TOKEN:-}" ] && [ -n "${HCLOUD_TOKEN_OP:-}" ]; then
  HCLOUD_TOKEN="$HCLOUD_TOKEN_OP"
fi

[ -n "${HCLOUD_TOKEN:-}" ] || {
  echo "HCLOUD_TOKEN is unset — sign into 1Password CLI or set it manually." >&2
  exit 1
}
export HCLOUD_TOKEN
