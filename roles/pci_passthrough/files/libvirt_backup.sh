#!/bin/bash
test -e /usr/local/lib/bash-framework && source /usr/local/lib/bash-framework || (echo "Could not load bash-framework" 1>&2; exit 1)

################################
#        SCRIPT CONFIG         #
################################

# Maximum time to wait for the domain to shutdown
MAXWAIT=60

# Maximum number of shutdown attempts
MAXTRIES=2

# Interval to wait between state checks during shutdown
INTERVAL=5

# Backup destination
BACKUP_DEST="/mnt/tank/backup"

################################
#          ACTUAL JOB          #
###############################@

log "Backup started"

DOMAINS=$(virsh list --all --name)

for DOMAIN in $DOMAINS; do
  br
  log "Backing up $DOMAIN ..."
  WAS_RUNNING=0

  if virsh dominfo "$DOMAIN" | grep 'State:' | grep -qv 'shut off'; then

    log "$DOMAIN is running, shutting down..."
    WAS_RUNNING=1

    WAIT=$MAXWAIT
    TRIES=$((MAXTRIES - 1))

    virsh shutdown "$DOMAIN" > /dev/null

    if [ "$DOMAIN" == "HTPC" ]; then
      # Windows needs two attemps for some reason...
      virsh shutdown "$DOMAIN" > /dev/null
    fi

    while virsh dominfo "$DOMAIN" | grep 'State:' | grep -qv 'shut off'; do

      if [ $WAIT -eq 0 ]; then
        if [ $TRIES -eq 0 ]; then
          log "Shutdown of $DOMAIN failed"
          pushover_error "Shutdown of $DOMAIN failed"
          exit 1
        else
          log "Waited $MAXWAIT secs for shutdown, vm is stuck? Trying again one time..."
          WAIT=$MAXWAIT
          TRIES=$((TRIES - 1))
          virsh shutdown "$DOMAIN"
        fi
      else
        log "Waiting $INTERVAL sec for shutdown..."
        WAIT=$((WAIT - INTERVAL))
        sleep $INTERVAL
      fi

    done

    log "Domain has shut down"

  fi

  DEVICES=$(virsh domblklist --inactive --details "$DOMAIN" | grep disk | awk '{ print $3}')
  for DEVICE in $DEVICES; do
    IMAGE=$(virsh domblklist --inactive "$DOMAIN" | grep "$DEVICE" | awk '{ print $2}')
    EXTENSION="${IMAGE##*.}"
    DEST_IMAGE="$BACKUP_DEST/$DOMAIN.$DEVICE.$EXTENSION"

    log "Backing up $DEVICE ($IMAGE -> $DEST_IMAGE)"
    cp --sparse=always "$IMAGE" "$DEST_IMAGE"
  done

  log "Dumping domain xml"
  virsh dumpxml --inactive "$DOMAIN" > "$BACKUP_DEST/$DOMAIN.xml"

  if [ $WAS_RUNNING -eq 1 ]; then
    log "Restarting $DOMAIN"
    virsh start "$DOMAIN" > /dev/null
  fi

  log "$DOMAIN is backed up"

done

deadmansnitch "a918d04d00"
log "Done"
exit 0
