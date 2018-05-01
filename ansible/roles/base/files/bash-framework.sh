#!/bin/bash

# Bash strict mode http://redsymbol.net/articles/unofficial-bash-strict-mode/
set -eu
set -o pipefail
IFS=$'\n\t'

# Override path, for inside cron
# Does not completes existing path to have identical environment when run manually
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

DEBUG=${DEBUG:-0}
XTRACE=${XTRACE:-0}
if [ "$XTRACE" == 1 ]; then
  DEBUG=1
  PS4='+(${BASH_SOURCE}:${LINENO}): ${FUNCNAME[0]:+${FUNCNAME[0]}(): }' # Add source, line & function to xtrace
  set -x
fi

NAME=$(basename "$0")

TMP_OUTPUT=$(mktemp "/tmp/$NAME.XXXXXXXX")
trap 'rm -rf $TMP_OUTPUT' EXIT

LOG_FILE="/var/log/$NAME.log"
function log()
{
  local MSG="$(date +%FT%T%z) $1"
  echo "$MSG" >> "$TMP_OUTPUT"
  echo "$MSG" >> "$LOG_FILE"
  if [ $DEBUG -eq "1" ]; then
    echo "$MSG"
  fi
}

function indent()
{
  while read line ; do
    log "    $line"
  done
}

function hr()
{
  log "--------------------------"
}

function br()
{
  log ""
}

function run()
{
  local IFS=" " # Needed to join params with a space
  local CMD="$*"

  log "Running \`$CMD\`"
  hr
  eval "stdbuf -i0 -o0 -e0 $CMD | indent" # stdbuf removes buffering to have correct timestamps
  RETVAL=$?
  wait
  hr
  br
  _=$(exit $RETVAL)
}

function must_run_as_root()
{
  local MSG=${1:-"This script must be run as root"}
  if [[ $EUID -ne 0 ]]; then
     echo "$MSG" 1>&2
     exit 1
  fi
}

function deadmansnitch()
{
  log "Pinging Dead Man's Snitch at id: $1"
  curl "https://nosnch.in/$1" &> /dev/null
}


function pushover_error()
{
  pushover --token aKBvgtffdKU5i7C2D9waJ5fi7JSGzS --user uzCHDLuNLNwnhFRGE4Cpn6goDsrDKo \
    --message "ERROR :: $1" --priority "1" --title "$NAME"
}
