#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

[ "$(id -u)" == "0" ] || { echo >&2 "I require root. Aborting"; exit 1; }

# Override path, for inside cron
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Testing schedule (index is the day, Monday=0, Sunday=6)
declare -a SCHEDULE=(
"/dev/disk/by-id/ata-WDC_WD100EMAZ-00WJTA0_2YKG8G2D"
"/dev/disk/by-id/ata-WDC_WD100EMAZ-00WJTA0_JEJYH3LZ"
"/dev/disk/by-id/ata-WDC_WD100EMAZ-00WJTA0_JEK2LK1Z"
"/dev/disk/by-id/ata-WDC_WD100EMAZ-00WJTA0_JEKG9J2Z"
"/dev/disk/by-id/ata-ST8000AS0002-1NA17Z_Z8403NE6"
"/dev/disk/by-id/ata-ST8000AS0002-1NA17Z_Z840AL1B"
"/dev/disk/by-id/ata-Crucial_CT240M500SSD1_13410953598E /dev/disk/by-id/ata-Crucial_CT240M500SSD1_14270C8904C3"
)

# Monday=0, Sunday=6
DAY=$(($(date '+%u') - 1))

if [ "${SCHEDULE[$DAY]}" == "" ]; then
  echo "No devices for today ($(date '+%A'))"
else
  IFS=', ' read -a DEVICES <<< "${SCHEDULE[$DAY]}"
  echo "Devices for today ($(date '+%A')): ${SCHEDULE[$DAY]}"

  for device in "${DEVICES[@]}"; do
    echo "..."
    if [ -h $device ]; then
      smartctl -t long $device
    else
      echo "Skipping $device, does not exist"
    fi
  done
fi

echo "Done"
