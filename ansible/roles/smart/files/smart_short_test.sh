#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

[ "$(id -u)" == "0" ] || { echo >&2 "I require root. Aborting"; exit 1; }

# Override path, for inside cron
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

ls /dev/disk/by-path | grep -v usb | grep -v part | xargs -I{} readlink -f /dev/disk/by-path/{} | grep -v /dev/sr | while read -r device ; do
  smartctl -t short $device
  echo "Waiting 2 minutes for test on $device ..."
  sleep 120
  echo "$device done"
done

echo "Done"
