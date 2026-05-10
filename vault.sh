#!/bin/bash
# vault.sh -- return the ansible-vault password for a given vault-id.
#
# Called by ansible via `vault_identity_list` in ansible.cfg with the
# vault-id as $1 (e.g. `vault.sh prod` or `vault.sh test`). Backwards
# compat: invoked with no args, defaults to `prod` so any callsite still
# using `--vault-password-file vault.sh` keeps working.
#
# Lookup order per id:
#   1. HOMELAB_VAULT_PASSWORD_<UPPERCASE_ID> env var (CI uses this for
#      `test`, populated from a Gitea repo secret).
#   2. macOS keychain entry `homelab-vault-<id>`.
#   3. Linux file `~/.config/homelab/vault-pass-<id>`, mode 0400.
set -euo pipefail

id="${1:-prod}"
case "$id" in
  prod | test) ;;
  *)
    echo "vault.sh: unknown vault-id '$id' (expected: prod, test)" >&2
    exit 1
    ;;
esac

upper=$(printf '%s' "$id" | tr '[:lower:]' '[:upper:]')
env_var="HOMELAB_VAULT_PASSWORD_${upper}"
if [ -n "${!env_var:-}" ]; then
  printf '%s' "${!env_var}"
  exit 0
fi

if [ "$(uname)" = "Darwin" ]; then
  # set the password with: security add-generic-password -a ak \
  #   -j "vault password ansible (<id>)" -s homelab-vault-<id> -w
  # Silence "could not be found" stderr so missing identities are quiet
  # (ansible interprets exit 1 as "no password for this id" and moves
  # on); real failures like a locked keychain still bubble up if you
  # check $? interactively.
  /usr/bin/security find-generic-password -a ak -s "homelab-vault-${id}" -w 2>/dev/null
else
  # set the password with: install -m 0400 /dev/stdin \
  #   "$HOME/.config/homelab/vault-pass-<id>" <<<"$pw"
  pass_file="$HOME/.config/homelab/vault-pass-${id}"
  # Quiet exit when the file simply isn't there (e.g. CI for prod).
  # ansible logs a benign warning and skips the identity. Mode check
  # only runs for files that DO exist, so a sloppy mode is still loud.
  [ -f "$pass_file" ] || exit 1
  mode=$(stat -c '%a' "$pass_file")
  if ((8#$mode & 0077)); then
    echo "vault.sh: refusing to read $pass_file with mode $mode; run: chmod 0400 $pass_file" >&2
    exit 1
  fi
  cat "$pass_file"
fi
