#!/usr/bin/env bash
set -euo pipefail

has_keys=false
for file in /etc/ssh/ssh_host_*; do
  if [ -f "$file" ]; then
    has_keys=true
    break
  fi
done

# Generate Host keys, if required
if [ $has_keys = false ]; then
  ssh-keygen -A
fi

exec "$@"
