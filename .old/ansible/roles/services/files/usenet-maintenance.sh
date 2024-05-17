#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

sed -i 's/[ \t]*$//' /mnt/services/sabnzbd/autoProcessMedia.cfg

find /mnt/media -maxdepth 1 -mindepth 1 -type d -not -name ".tmp" -print0 | xargs -0 chown -R ak:ak
find /mnt/media_unsafe -maxdepth 1 -mindepth 1 -type d -not -name ".tmp" -print0 | xargs -0 chown -R ak:ak

dms 150c9a2135
