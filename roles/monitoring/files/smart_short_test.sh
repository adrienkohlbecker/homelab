#!/bin/bash
test -e /usr/local/lib/bash-framework && source /usr/local/lib/bash-framework || (echo "Could not load bash-framework" 1>&2; exit 1)

################################
#          ACTUAL JOB          #
###############################@

must_run_as_root

br
log "Smart short test started."
br

shopt -s nullglob
DEVICES=(/dev/[shv]d?)
shopt -u nullglob

for device in "${DEVICES[@]}"; do

  run "smartctl -t short $device"
  log "Waiting 2 minutes for test on $device ..."
  sleep 120
  log "$device done"

done

curl https://nosnch.in/4e614313f5
log "Done"
exit 0
