#!/bin/bash
set -euo pipefail

# enable-by-path links /data/wol_relay/udm_wol_relay.service back into
# /etc/systemd/system, reloads, enables, and starts in one documented step.
systemctl enable --now /data/wol_relay/udm_wol_relay.service
