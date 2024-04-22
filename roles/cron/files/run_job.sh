#!/bin/bash
set -euo pipefail

usage() {
  echo >&2 "Usage: run_job (daily|weekly|monthly) IDENTIFIER CMD [ARGS...]"
}

frequency=${1:-}
if [ "$frequency" != "hourly" ] && [ "$frequency" != "daily" ] && [ "$frequency" != "weekly" ] && [ "$frequency" != "monthly" ]; then
  usage
  exit 1
fi
shift

identifier=${1:-}
if [ "$identifier" = "" ]; then
  usage
  exit 1
fi
shift

if [ "$*" = "" ]; then
  usage
  exit 1
fi

/usr/bin/systemd-cat --identifier "$identifier" --stderr-priority=3 "$@" && echo "$frequency $(/usr/bin/date --iso-8601=seconds)" >"/var/log/jobs/$identifier"
