#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

[ "$(id -u)" == "0" ] || { echo >&2 "I require root. Aborting"; exit 1; }

shopt -s nullglob
DEVICES=(/dev/[shv]d?)
shopt -u nullglob

for device in "${DEVICES[@]}"; do

  smartctl -t short $device
  echo "Waiting 2 minutes for test on $device ..."
  sleep 120
  echo "$device done"

done

echo "Done"
