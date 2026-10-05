#!/bin/bash
set -euo pipefail
# Reinstall keepalived after a UniFi OS firmware update wipes /usr. Its
# config, healthcheck and heartbeat units live in /etc, which the update
# keeps, so only the package needs restoring.
if ! command -v keepalived >/dev/null; then
  apt-get update -qq
  apt-get install -y -qq keepalived
fi
keepalived -t -f /etc/keepalived/keepalived.conf
systemctl enable --now keepalived.service
