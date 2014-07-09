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

function pushover_log() {

  local PRIORITY=${2:-"0"}

  pushover --token ab8o6HMdRRVhNCeUHsTaAKovTxcJxC --user uzCHDLuNLNwnhFRGE4Cpn6goDsrDKo \
    --message "$1" --priority "$PRIORITY" --title "ZFS Health"

}

must_run_as_root

br
log "Zfs health check started."
br

run "zpool status 2>&1"
run "zpool list 2>&1"
run "zfs list -r 2>&1"

# Health - Check if all zfs volumes are in good condition. We are looking for
# any keyword signifying a degraded or broken array.
log "Checking pool health condition..."
if zpool status | grep -q -e 'DEGRADED\|FAULTED\|OFFLINE\|UNAVAIL\|REMOVED\|FAIL\|DESTROYED\|corrupt\|cannot\|unrecover'; then

  log "ERROR :: Detected pool health fault"
  mail -s "$EMAIL_SUBJECT_PREFIX - Health fault" "$EMAIL_TO" < "$TMP_OUTPUT"
  pushover_log "ERROR :: Detected pool health fault" "1"
  exit 1
fi

# Errors - Check the columns for READ, WRITE and CKSUM (checksum) drive errors
# on all volumes and all drives using "zpool status". If any non-zero errors
# are reported an email will be sent out. You should then look to replace the
# faulty drive and run "zpool scrub" on the affected volume after resilvering.
log "Checking drive errors..."
if zpool status | grep ONLINE | grep -v state | awk '{print $3 $4 $5}' | grep -qv 000; then
  log "ERROR :: Detected drive errors"
  mail -s "$EMAIL_SUBJECT_PREFIX - Drive errors" "$EMAIL_TO" < "$TMP_OUTPUT"
  pushover_log "ERROR :: Detected drive errors" "1"
  exit 1
fi

# Scrub Expired - Check if all volumes have been scrubbed in at least the last
# 8 days. The general guide is to scrub volumes on desktop quality drives once
# a week and volumes on enterprise class drives once a month. You can always
# use cron to schedule "zpool scrub" in off hours. We scrub our volumes every
# Sunday morning for example.
#
# Scrubbing traverses all the data in the pool once and verifies all blocks can
# be read. Scrubbing proceeds as fast as the devices allows, though the
# priority of any I/O remains below that of normal calls. This operation might
# negatively impact performance, but the file system will remain usable and
# responsive while scrubbing occurs. To initiate an explicit scrub, use the
# "zpool scrub" command.
log "Checking scrub age..."

CURRENT_DATE=$(date +"%s")
ZFS_VOLUMES=$(zpool list -H -o name)

for volume in $ZFS_VOLUMES; do
  if zpool status "$volume" | grep -q "none requested"; then
    log "ERROR :: No scrub requested on $volume"
    mail -s "$EMAIL_SUBJECT_PREFIX - No scrub requested on $volume" "$EMAIL_TO" < "$TMP_OUTPUT"
    pushover_log "ERROR :: No scrub requested on $volume" "1"
    exit 1
  elif zpool status "$volume" | grep -q -e "scrub canceled"; then
    log "ERROR :: Last scrub canceled on $volume"
    mail -s "$EMAIL_SUBJECT_PREFIX - Last scrub canceled on $volume" "$EMAIL_TO" < "$TMP_OUTPUT"
    pushover_log "ERROR :: Last scrub canceled on $volume" "1"
    exit 1
  elif zpool status "$volume" | grep -q -e "scrub in progress\|resilver"; then
    log "Scrub in progress for $volume, skipping."
    break
  fi

  SCRUB_RAW_DATE=$(zpool status "$volume" | grep scrub | awk '{print $11" "$12" " $13" " $14" "$15}')
  SCRUB_DATE=$(date -d "$SCRUB_RAW_DATE" +"%s")

  if [ $((CURRENT_DATE - SCRUB_DATE)) -ge $SCRUB_EXPIRE ]; then
    log "ERROR :: Scrub expired on $volume"
    mail -s "$EMAIL_SUBJECT_PREFIX - Scrub expired on $volume" "$EMAIL_TO" < "$TMP_OUTPUT"
    pushover_log "ERROR :: Scrub expired on $volume" "1"
    exit 1
  fi
done

log "Done"
exit 0
