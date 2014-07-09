#!/bin/bash
test -e /usr/local/lib/bash-framework && source /usr/local/lib/bash-framework || (echo "Could not load bash-framework" 1>&2; exit 1)

################################
#        SCRIPT CONFIG         #
################################

# Destination email
EMAIL_TO="root"

# Email subject prefix
EMAIL_SUBJECT_PREFIX="[$(hostname -s)] $NAME"

################################
#          ACTUAL JOB          #
###############################@

function pushover_log() {

  local PRIORITY=${2:-"0"}

  pushover --token aALyPPoeQ8g1uKApyJgKLYYAMaPPmx --user uzCHDLuNLNwnhFRGE4Cpn6goDsrDKo \
    --message "$1" --priority "$PRIORITY" --title "Snapraid scrub"

}

must_run_as_root

br
log "Snapraid scrub started."
br

run "snapraid scrub 2>&1"

if grep -q "Everything OK" "$TMP_OUTPUT"; then
  log "Everything looks good"
  pushover_log "OK :: Scrub finished sucessfully" "-1"
  exit 0
elif grep -q "WARNING! There are errors" "$TMP_OUTPUT"; then
  READ_COUNT=$(grep '[0-9]\{1,\} read errors$' "$TMP_OUTPUT" | sed 's/ \+/ /g' | cut -d ' ' -f2)
  DATA_COUNT=$(grep '[0-9]\{1,\} data errors$' "$TMP_OUTPUT" | sed 's/ \+/ /g' | cut -d ' ' -f2)

  log "Scrub errors summary: Read [$READ_COUNT] - Data [$DATA_COUNT]"
  mail -s "$EMAIL_SUBJECT_PREFIX - Errors found during scrub" "$EMAIL_TO" < "$TMP_OUTPUT"
  pushover_log "ERROR :: Errors found during scrub" "1"
  exit 1
else
  log "An unexpected error has happened."
  mail -s "$EMAIL_SUBJECT_PREFIX ERROR - An unexpected error has happended during scrub" "$EMAIL_TO" < "$TMP_OUTPUT"
  pushover_log "ERROR :: Unexpected error during scrub" "1"
  exit 1
fi
