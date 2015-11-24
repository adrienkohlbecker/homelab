#!/bin/bash
test -e /usr/local/lib/bash-framework && source /usr/local/lib/bash-framework || (echo "Could not load bash-framework" 1>&2; exit 1)

################################
#          ACTUAL JOB          #
###############################@

must_run_as_root

br
log "Service update started."
br

cd /opt/services

log "Stopping service."
run "systemctl stop compose 2>&1"

log "Pulling compose repository"
run "git pull 2>&1"

log "Pulling images."
run "/usr/local/bin/docker-compose pull 2>&1"

log "Starting service."
run "systemctl start compose 2>&1"

deadmansnitch "1c61a35df2"
log "Done"
exit 0
