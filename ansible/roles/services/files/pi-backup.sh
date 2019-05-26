#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

rsync -va -e "ssh -i /etc/pi_backup_rsa"  homelab@10.123.0.15:. /mnt/services/pi
