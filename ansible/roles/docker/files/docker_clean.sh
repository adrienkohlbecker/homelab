#!/bin/bash
test -e /usr/local/lib/bash-framework && source /usr/local/lib/bash-framework || (echo "Could not load bash-framework" 1>&2; exit 1)

################################
#          ACTUAL JOB          #
###############################@

must_run_as_root

br
log "Docker clean started."
br

run "docker system prune -f 2>&1"

deadmansnitch "98d0602056"
log "Done"
exit 0
