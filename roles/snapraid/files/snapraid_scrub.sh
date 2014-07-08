#!/bin/bash
test -e /usr/local/lib/bash-framework && source /usr/local/lib/bash-framework || (echo "Could not load bash-framework" 1>&2; exit 1)

################################
#        SCRIPT CONFIG         #
################################

# Threshold of deleted files over which to show an error
# Useful in case of mass deletion by human error / missing device
DEL_THRESHOLD=50

# Destination email
EMAIL_TO="root"

# Email subject prefix
EMAIL_SUBJECT_PREFIX="[$(hostname -s)] $NAME - "

################################
#          ACTUAL JOB          #
###############################@

function dogevent_log()
{
  dogevent --title "$NAME - $1" \
    --file "$TMP_OUTPUT" \
    --alert_type "${2:-info}" \
    --priority "${3:-normal}" \
    --tag "application:$NAME" \
    --host "$(hostname)" \
    --aggregation_key "$NAME - $1"
}

must_run_as_root

br
log "snapraid scrub started."

run "snapraid scrub -p1 2>&1"

if [ grep -q "Everything OK" $TMP_OUTPUT ]; then
  log "Everything looks good"
  dogevent_log "Scrub finished sucessfully" "success" "low"
  exit 0
elif [ grep -q "WARNING! There are errors" $TMP_OUTPUT]; then
  READ_COUNT=$(grep '[0-9]\{1,\} read errors$' "$TMP_OUTPUT" | sed 's/ \+/ /g' | cut -d ' ' -f2)
  DATA_COUNT=$(grep '[0-9]\{1,\} data errors$' "$TMP_OUTPUT" | sed 's/ \+/ /g' | cut -d ' ' -f2)

  log "Scrub errors summary: Read [$READ_COUNT] - Data [$DATA_COUNT]"
  mail -s "$EMAIL_SUBJECT_PREFIX - Errors found during scrub" "$EMAIL_TO" < $TMP_OUTPUT
  dogevent_log "Errors found during scrub" "error"
  exit 1
else
  log "An unexpected error has happened."
  mail -s "$EMAIL_SUBJECT_PREFIX ERROR - An unexpected error has happended during scrub" "$EMAIL_TO" < $TMP_OUTPUT
  dogevent_log "Unexpected error during scrub" "error"
  exit 1
fi
