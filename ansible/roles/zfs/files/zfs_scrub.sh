#!/bin/bash
test -e /usr/local/lib/bash-framework && source /usr/local/lib/bash-framework || (echo "Could not load bash-framework" 1>&2; exit 1)

################################
#        SCRIPT CONFIG         #
################################

# Destination email
EMAIL_TO="root"

# Email subject prefix
EMAIL_SUBJECT_PREFIX="[$(hostname -s)] $NAME"

# Scrub expiration time in seconds. So for 8 days we calculate 8 days
# times 24 hours times 3600 seconds to equal 691200 seconds.
SCRUB_EXPIRE=691200

################################
#          ACTUAL JOB          #
###############################@

must_run_as_root

br
log "Zfs scrub started."
br

# find all pools
POOLS=$(zpool list -H -o name | grep -v backup-A | grep -v backup-B)
POOL_LIST=$(echo $POOLS | tr "\\n" ", " | sed 's/,$//')
log "Pool list: $POOL_LIST"
br

# for each pool
for pool in $POOLS; do
  # start scrub for $pool
  run "zpool scrub $pool 2>&1"
  RUNNING=1
  sleep 5
  # wait until scrub for $pool has finished running
  while [ $RUNNING = 1 ]; do
    # still running?
    STATUS=$(zpool status -v $pool)
    if echo "$STATUS" | grep -q "scrub in progress"; then
        TOGO=$(echo "$STATUS" | grep scanned | awk '{print $8}')
        log "Scrub in progress ($TOGO to go)... Sleeping 5 minutes."
        sleep 300
    # not running
    else
      # finished with this pool, exit
      log "Scrub ended on $pool"
      br
      run "zpool status -v $pool 2>&1"
      br
      RUNNING=0
    fi
  done
done

deadmansnitch "ac9e9c5a77"
log "Done"
exit 0
