#!/bin/bash
# shellcheck source=../../bash/files/functions.sh
source /usr/local/lib/functions.sh

usage() {
  f_error "Usage: run_job (hourly|daily|weekly|monthly) IDENTIFIER CMD [ARGS...]"
}

if [ "$#" -lt 3 ]; then
  usage
  exit 1
fi

frequency=$1
identifier=$2
shift 2

case $frequency in
hourly)
  TIMEOUT=3300 # 55 minutes
  ;;
daily)
  TIMEOUT=83130 # 23 hours and 55 minutes
  ;;
weekly)
  TIMEOUT=601530 # 6 days 23 hours and 55 minutes
  ;;
monthly)
  TIMEOUT=2415930 # 27 days 23 hours and 55 minutes
  ;;
*)
  usage
  exit 1
  ;;
esac

/usr/bin/systemd-cat --identifier "$identifier" --stderr-priority=3 /usr/bin/timeout --kill-after=120 $TIMEOUT "$@" && echo "$frequency $(/usr/bin/date --iso-8601=seconds)" >"/var/log/jobs/$identifier"
