#!/bin/bash
test -e /usr/local/lib/bash-framework && source /usr/local/lib/bash-framework || (echo "Could not load bash-framework" 1>&2; exit 1)

################################
#        SCRIPT CONFIG         #
################################

# Testing schedule (index is the day, Monday=0, Sunday=6)
declare -a SCHEDULE=(
  "/dev/disk/by-id/ata-WDC_WD10JFCX-68N6GN0_WD-WXA1E63DJRT8 /dev/disk/by-id/ata-ST8000AS0002-1NA17Z_Z8403NE6"
  "/dev/disk/by-id/ata-WDC_WD10JFCX-68N6GN0_WD-WXA1E63DLZR6 /dev/disk/by-id/ata-ST8000AS0002-1NA17Z_Z840AL1B"
  "/dev/disk/by-id/ata-WDC_WD10JFCX-68N6GN0_WD-WXA1E63EVZN6 /dev/disk/by-id/ata-Hitachi_HDS5C3030BLE630_MC49215X0B7AYA"
  "/dev/disk/by-id/ata-WDC_WD10JFCX-68N6GN0_WD-WXA1E63EXDD4 /dev/disk/by-id/ata-Hitachi_HDS5C3030BLE630_MC49215X0B7DNA"
  "/dev/disk/by-id/ata-WDC_WD10JFCX-68N6GN0_WD-WXA1E63FXKH5 /dev/disk/by-id/ata-WDC_WD30EZRX-00D8PB0_WD-WMC4N1263858"
  "/dev/disk/by-id/ata-WDC_WD10JFCX-68N6GN0_WD-WXN1E53SA095 /dev/disk/by-id/ata-WDC_WD30EZRX-00SPEB0_WD-WCC4E1229185"
  "/dev/disk/by-id/ata-Crucial_CT240M500SSD1_13410953598E /dev/disk/by-id/ata-Crucial_CT240M500SSD1_14230C42B78B"
)

################################
#          ACTUAL JOB          #
###############################@

must_run_as_root

br
log "Smart long test started."
br

# Monday=0, Sunday=6
DAY=$(($(date '+%u') - 1))

if [ "${SCHEDULE[$DAY]}" == "" ]; then
  log "No devices for today ($(date '+%A'))"
else
  IFS=', ' read -a DEVICES <<< "${SCHEDULE[$DAY]}"
  log "Devices for today ($(date '+%A')): ${SCHEDULE[$DAY]}"

  for device in "${DEVICES[@]}"; do
    echo "..."
    run "smartctl -t long $device 2>&1"
  done
fi

deadmansnitch "39a52f20f7"
log "Done"
exit 0
