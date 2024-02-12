#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

rsync -va -e "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i /etc/pi_backup_rsa" --delete homelab@10.123.0.15:. /mnt/beelink_services
