#!/bin/bash
# Netplan auto-rollback script invoked by netplan_rollback.service.
# The corresponding .timer fires ~90s after a safe-apply arms it.
# If the apply succeeded, ansible touches /run/netplan_keep before
# the timer fires; we then exit without rolling back. Otherwise we
# restore /run/netplan_prev (snapshot taken pre-template) over
# /etc/netplan and re-apply.
set -euo pipefail
if [ -f /run/netplan_keep ]; then
  rm -f /run/netplan_keep
  logger -t netplan_rollback "cancelled — apply confirmed OK"
  exit 0
fi

# Refuse to touch /etc/netplan unless we actually have a snapshot to
# restore. A missing snapshot (tmpfs wiped, snapshot died mid-converge,
# a --start-at-task resume that skipped it) would otherwise leave the
# box with no netplan config at all -- a worse lockout than the bad
# config we are rolling back from.
if [ ! -d /run/netplan_prev ]; then
  logger -p user.err -t netplan_rollback "ABORT: no /run/netplan_prev snapshot; leaving /etc/netplan untouched"
  exit 1
fi

logger -p user.err -t netplan_rollback "auto-rollback firing: restoring previous /etc/netplan"
# Stash the presumed-broken config aside rather than deleting it, so a
# restore that fails part-way is still recoverable from the console.
# The final mv onto /etc/netplan is the last step.
rm -rf /etc/netplan.rollback_failed
mv /etc/netplan /etc/netplan.rollback_failed
mv /run/netplan_prev /etc/netplan
netplan apply
