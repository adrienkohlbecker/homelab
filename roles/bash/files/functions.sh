#!/bin/bash
set -euo pipefail

if [ -n "${F_DEBUGME:-}" ]; then

  # Open file descriptor to log file
  exec 5>"$(mktemp -t "bash-$(basename "${BASH_SOURCE[0]}")-$(printf "%(%Y%m%d-%H%M%S)T" -2)-XXXXX.log")"
  # Set prefix used when echoing trace information `source.sh(pid) isodate lineno: `
  PS4='+$(printf "%(%FT%T%z)T %3s" -1 $LINENO): '
  # Send output of trace to file descriptor number 5 (our log file)
  BASH_XTRACEFD="5"
  # Enable trace mode
  set -x

fi
