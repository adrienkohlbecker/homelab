#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

/usr/local/bin/sort-ini /mnt/services/sabnzbd/sabnzbd.ini
/usr/local/bin/sort-ini /mnt/services/headphones/config.ini
/usr/local/bin/sort-ini /mnt/services/couchpotato/settings.conf

sed -i 's/[ \t]*$//' /mnt/services/sabnzbd/autoProcessMedia.cfg

find /mnt/media -maxdepth 1 -mindepth 1 -type d -not -name ".tmp" -print0 | xargs -0 chown -R ak:ak

dms 150c9a2135
