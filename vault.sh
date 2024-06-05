#!/bin/bash
set -euo pipefail

# set the password with security add-generic-password -a ak -j "vault password ansible" -s homelab-vault -w
/usr/bin/security find-generic-password -a ak -s homelab-vault -w
