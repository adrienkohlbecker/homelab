#!/bin/bash
set -euo pipefail

if [ $(uname) = "Darwin" ]; then
  # set the password with security add-generic-password -a ak -j "vault password ansible" -s homelab-vault -w
  /usr/bin/security find-generic-password -a ak -s homelab-vault -w
else
  # set the password with: install -m 0400 /dev/stdin "$HOME/.config/homelab/vault-pass" <<<"$pw"
  pass_file="$HOME/.config/homelab/vault-pass"
  mode=$(stat -c '%a' "$pass_file")
  if (( 8#$mode & 0077 )); then
    echo "vault.sh: refusing to read $pass_file with mode $mode; run: chmod 0400 $pass_file" >&2
    exit 1
  fi
  cat "$pass_file"
fi
