#!/bin/bash
set -euo pipefail

usage() {
  echo >&2 "Usage: run_job IDENTIFIER CMD [ARGS...]"
}

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

/usr/bin/systemd-cat --identifier "$identifier" "$@" && /usr/bin/date --iso-8601=seconds >"/var/log/jobs/$identifier"
