#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

/usr/local/bin/sort-ini /mnt/docker/sickrage/config.ini
/usr/local/bin/sort-ini /mnt/docker/sabnzbd/sabnzbd.ini
/usr/local/bin/sort-ini /mnt/docker/couchpotato/settings.conf
find /mnt/media -maxdepth 1 -mindepth 1 -type d -not -name ".tmp" -print0 | xargs -0 chown adrien:adrien
