#!/usr/bin/env bash
#MISE description="Mirror the working tree and the test Ansible vault password to a remote builder (default lab) for on-host packer/ansible runs -- e.g. the fox image bake, which is qemu/KVM and can't run on the Mac. Honours every .gitignore, so the notes/ private clone, .venv, and build artifacts never ship."
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

# The `test` vault password, so the harness can decrypt group_vars/test.yml when
# run on the remote builder. Read up-front so a missing identity fails before the
# multi-minute mirror rather than after it -- vault-client.sh exits 1 *silently*
# for an unconfigured id (a locked keychain looks the same), so say what is wrong.
#
# `prod` is deliberately never shipped: nothing on the remote consumes it (packer
# bakes touch no vault, and the fox converge runs from the workstation -- see
# notes/runbooks/fox_rebuild.md Phase 6), while lab hosts the lab-shell-qemu
# GitLab runner. AGENTS.md "Vault ids" scopes prod to local workstations only.
if [ "${usage_dry_run:-false}" != "true" ]; then
  if ! test_vault_password=$(./vault-client.sh test) || [ -z "$test_vault_password" ]; then
    echo "lab:sync: no 'test' vault password available -- locked keychain, or the" >&2
    echo "          identity is not configured. See notes/runbooks/vault_setup.md" >&2
    exit 1
  fi
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
  # Single-quoted so the remote shell -- not this one -- expands $HOME; the
  # password arrives on stdin and never appears in argv or the process table.
  # install(1) unlinks the destination first, so re-running over the existing
  # 0400 file succeeds.
  printf '%s' "$test_vault_password" | ssh "$host" '
    set -eu
    vault_dir="$HOME/.config/homelab"
    install -d -m 0700 "$vault_dir"
    install -m 0400 /dev/stdin "$vault_dir/vault-pass-test"
  '

  cat <<EOF

==> Synced to ${host}:${dest}, and installed the test vault password at
    ~/.config/homelab/vault-pass-test (prod is never shipped).
    To bake fox's image there (see notes/runbooks/fox_rebuild.md):
    ssh ${host}
    cd ${dest} && mise trust && mise install && mise run packer:init
    mise run packer:build hetzner --ubuntu noble
    mise run packer:hetzner       --ubuntu noble
EOF
fi
