#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

[ "$(id -u)" == "0" ] || { echo >&2 "I require root. Aborting"; exit 1; }

# Override path, for inside cron
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

docker system prune -f
