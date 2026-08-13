#!/usr/bin/env bash
#MISE description="Mirror the working tree and Ansible vault passwords to a remote builder (default lab) for on-host packer/ansible runs -- e.g. the fox image bake, which is qemu/KVM and can't run on the Mac. Honours every .gitignore, so the notes/ private clone, .venv, and build artifacts never ship."
#USAGE flag "--host <host>" help="ssh destination to mirror onto" default="lab"
#USAGE flag "--dest <dest>" help="Path on the remote; relative paths are under the remote home" default="homelab"
#USAGE flag "--dry-run" help="Preview the transfer without writing anything on the remote"
# shellcheck disable=SC2154  # usage_* vars are injected by mise from the #USAGE spec
set -euo pipefail

# Anchor at the repo root regardless of where mise was invoked, so the rsync
# source (.) and the per-directory .gitignore merge are correct. In a worktree
# this is the worktree root -- you sync the tree you are working in.
cd "$(git rev-parse --show-toplevel)"

host="${usage_host}"
dest="${usage_dest}"

install_vault_password() {
  local vault_id=$1 password=$2

  # vault_id is a fixed local prod/test identifier used to select the remote file.
  # shellcheck disable=SC2029
  printf '%s' "$password" | ssh "$host" "
    set -eu
    vault_dir=\"\$HOME/.config/homelab\"
    install -d -m 0700 \"\$vault_dir\"
    tmp=\$(mktemp \"\$vault_dir/.vault-pass-${vault_id}.XXXXXX\")
    trap 'rm -f \"\$tmp\"' EXIT HUP INT TERM
    cat >\"\$tmp\"
    test -s \"\$tmp\"
    chmod 0400 \"\$tmp\"
    mv \"\$tmp\" \"\$vault_dir/vault-pass-${vault_id}\"
    trap - EXIT HUP INT TERM
  "
}

if [ "${usage_dry_run:-false}" != "true" ]; then
  prod_vault_password=$(./vault-client.sh prod)
  test_vault_password=$(./vault-client.sh test)
  [ -n "$prod_vault_password" ]
  [ -n "$test_vault_password" ]
fi

# Mirror the tree:
#  --filter dir-merge .gitignore -> skip everything git ignores, per directory
#     (notes/ private clone, .venv, packer/artifacts, test/out, ...). Excluded
#     paths are also protected from --delete on the receiver, so they survive.
#  --exclude /out.log /vault.sh  -> local-only files .gitignore does not cover.
#  --delete                      -> the remote is a throwaway mirror; prune stale.
rsync_args=(
  -vah --progress --delete
  --exclude="/out.log" --exclude="/vault.sh"
  --filter="dir-merge,- .gitignore"
)
if [ "${usage_dry_run:-false}" = "true" ]; then
  rsync_args+=(--dry-run)
  echo "==> DRY RUN: previewing sync to ${host}:${dest} (nothing will be written)"
fi

rsync "${rsync_args[@]}" . "${host}:${dest}"

if [ "${usage_dry_run:-false}" != "true" ]; then
  install_vault_password prod "$prod_vault_password"
  install_vault_password test "$test_vault_password"

  cat <<EOF

==> Synced to ${host}:${dest} and installed the Ansible vault passwords.
    To bake fox's image there (see notes/runbooks/fox_rebuild.md):
    ssh ${host}
    cd ${dest} && mise trust && mise install && mise run packer:init
    mise run packer:build hetzner --ubuntu noble
    mise run packer:hetzner       --ubuntu noble
EOF
fi
