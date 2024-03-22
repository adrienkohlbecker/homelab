#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

# ensure the primary is up
dig @10.123.0.16 google.fr

# stop secondary pihole
systemctl stop pihole
sleep 10

# sync from primary
rsync -va -e "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i /etc/pi_backup_rsa" --delete pi@10.123.0.11:. /mnt/services/pihole

# restart secondary pihole
systemctl restart pihole
