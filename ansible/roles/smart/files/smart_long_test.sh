#!/bin/bash
# http://redsymbol.net/articles/unofficial-bash-strict-mode/
IFS=$'\n\t'
set -euxo pipefail

[ "$(id -u)" == "0" ] || { echo >&2 "I require root. Aborting"; exit 1; }

# Override path, for inside cron
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Testing schedule (index is the day, Monday=0, Sunday=6)
declare -a SCHEDULE=(
  "/dev/disk/by-id/ata-WDC_WD10JFCX-68N6GN0_WD-WXA1E63DJRT8 /dev/disk/by-id/ata-ST8000AS0002-1NA17Z_Z8403NE6"
  "/dev/disk/by-id/ata-WDC_WD10JFCX-68N6GN0_WD-WXA1E63DLZR6 /dev/disk/by-id/ata-ST8000AS0002-1NA17Z_Z840AL1B"
  "/dev/disk/by-id/ata-WDC_WD10JFCX-68N6GN0_WD-WXA1E63EVZN6 /dev/disk/by-id/ata-Hitachi_HDS5C3030BLE630_MC49215X0B7AYA"
  "/dev/disk/by-id/ata-WDC_WD10JFCX-68N6GN0_WD-WXA1E63EXDD4 /dev/disk/by-id/ata-Hitachi_HDS5C3030BLE630_MC49215X0B7DNA"
  "/dev/disk/by-id/ata-WDC_WD10JFCX-68N6GN0_WD-WXA1E63FXKH5 /dev/disk/by-id/ata-WDC_WD30EZRX-00D8PB0_WD-WMC4N1263858"
  "/dev/disk/by-id/ata-WDC_WD10JFCX-68N6GN0_WD-WXN1E53SA095 /dev/disk/by-id/ata-WDC_WD30EZRX-00SPEB0_WD-WCC4E1229185"
  "/dev/disk/by-id/ata-Crucial_CT240M500SSD1_13410953598E /dev/disk/by-id/ata-Crucial_CT240M500SSD1_14230C42B78B /dev/disk/by-id/ata-WDC_WD20EZRX-00D8PB0_WD-WMC4M0859498"
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
    smartctl -t long $device
  done
fi

echo "Done"
