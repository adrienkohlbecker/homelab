#!/bin/bash
set -euo pipefail

# Set path to a known value, for use in CRON scripts
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# What is the executing script (sourcing this file) named
F_SCRIPT=$(basename "$0")

# Print an error message to stderr
f_error() {
  echo "$@" >&2
}

# Print an error message to stderr and exit
f_fail() {
  echo "$@" >&2
  exit 1
}

# If F_DEBUGME is set, enable tracing and save output to a temp file
if [ -n "${F_DEBUGME:-}" ]; then
  # Open file descriptor to log file
  exec 5>"$(mktemp -t "bash-$F_SCRIPT-$(printf "%(%Y%m%d-%H%M%S)T" -2)-XXXXX.log")"
  # Set prefix used when echoing trace information `source.sh(pid) isodate lineno: `
  PS4='+$(printf "%(%FT%T%z)T %3s" -1 $LINENO): '
  # Send output of trace to file descriptor number 5 (our log file)
  BASH_XTRACEFD="5"
  # Enable trace mode
  set -x
fi

# Use f_rescue to run multiple commands that can each fail, and observe the number in f_failed afterwards to know if any did
f_failed=0
f_rescue() {
  set +e
  "$@"
  retval=$?
  set -e

  if [ $retval -ne 0 ]; then
    f_failed=1
  fi
}

# Require being a root user
f_require_root() {
  if [ "$EUID" != "0" ]; then
    f_fail "Error: I require root"
  fi
}
