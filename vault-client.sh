#!/bin/bash
#
# Ansible passes the vault id only to scripts whose name ends in `-client`,
# so this one serves both `prod` and `test`. A positional id remains supported
# for ad-hoc use.
# Lookup order: HOMELAB_VAULT_PASSWORD_<UPPER_ID>, macOS keychain, Linux pass file.
set -euo pipefail

id=""
while [ $# -gt 0 ]; do
  case "$1" in
  --vault-id)
    id="${2:-}"
    shift 2
    ;;
  --vault-id=*)
    id="${1#--vault-id=}"
    shift
    ;;
  *)
    [ -z "$id" ] && id="$1"
    shift
    ;;
  esac
done
id="${id:-prod}"

case "$id" in
prod | test) ;;
*)
  echo "vault-client.sh: unknown vault-id '$id' (expected: prod, test)" >&2
  exit 1
  ;;
esac

env_var="HOMELAB_VAULT_PASSWORD_$(printf '%s' "$id" | tr '[:lower:]' '[:upper:]')"
if [ -n "${!env_var:-}" ]; then
  printf '%s' "${!env_var}"
  exit 0
fi

if [ "$(uname)" = "Darwin" ]; then
  # Set with: security add-generic-password -a ak \
  #   -j "vault password ansible (<id>)" -s homelab-vault-<id> -w
  # Silence "could not be found" stderr so missing identities are quiet
  # (ansible interprets exit 1 as "no password for this id" and moves
  # on); real failures like a locked keychain still bubble up if you
  # check $? interactively.
  /usr/bin/security find-generic-password -a ak -s "homelab-vault-${id}" -w 2>/dev/null
else
  # Set with: install -m 0400 /dev/stdin \
  #   "$HOME/.config/homelab/vault-pass-<id>" <<<"$pw"
  pass_file="$HOME/.config/homelab/vault-pass-${id}"
  # Quiet exit when the file simply isn't there (e.g. CI for prod).
  # ansible logs a benign warning and skips the identity. Mode check
  # only runs for files that DO exist, so a sloppy mode is still loud.
  [ -f "$pass_file" ] || exit 1
  mode=$(stat -c '%a' "$pass_file")
  if ((8#$mode & 0077)); then
    echo "vault-client.sh: refusing to read $pass_file with mode $mode; run: chmod 0400 $pass_file" >&2
    exit 1
  fi
  cat "$pass_file"
fi
