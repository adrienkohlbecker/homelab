#!/bin/bash
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install --yes ubuntu-standard ubuntu-server

for file in /etc/logrotate.d/*; do
  if grep -Eq "(^|[^#y])compress" "$file"; then
    sed -i -r "s/(^|[^#y])(compress)/\1#\2/" "$file"
  fi
done
