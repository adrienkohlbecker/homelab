#!/bin/bash
test -e /usr/local/lib/bash-framework && source /usr/local/lib/bash-framework || (echo "Could not load bash-framework" 1>&2; exit 1)

################################
#        SCRIPT CONFIG         #
################################

# Testing schedule (index is the day, Monday=0, Sunday=6)
declare -a SCHEDULE=(
  "/dev/sda /dev/sde"
  "/dev/sdb /dev/sdi"
  "/dev/sdc /dev/sdj"
  "/dev/sdf /dev/sdk"
  "/dev/sdg /dev/sdl"
  "/dev/sdh /dev/sdm"
  "/dev/sdd /dev/sdn"
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
