#!/bin/bash
test -e /usr/local/lib/bash-framework && source /usr/local/lib/bash-framework || (echo "Could not load bash-framework" 1>&2; exit 1)

################################
#          ACTUAL JOB          #
###############################@

must_run_as_root

br
log "Docker clean started."
br

STOPPED_CONTAINERS=$(docker ps -q -f "status=exited" | xargs)

if [[ $STOPPED_CONTAINERS == "" ]]; then
  log "No containers to cleanup"
else
  log "Cleaning exited containers"
  run "docker rm $STOPPED_CONTAINERS 2>&1"
fi

DANGLING_IMAGES=$(docker images -q --filter "dangling=true" | xargs)

if [[ $DANGLING_IMAGES == "" ]]; then
  log "No images to cleanup"
else
  log "Cleaning dangling images"
  run "docker rmi $DANGLING_IMAGES 2>&1"
fi

deadmansnitch "98d0602056"
log "Done"
exit 0
