#!/bin/bash
# Netplan auto-rollback script invoked by netplan_rollback.service.
# The corresponding .timer fires ~90s after a safe-apply arms it.
# If the apply succeeded, ansible touches /run/netplan_keep before
# the timer fires; we then exit without rolling back. Otherwise we
# restore /run/netplan_prev (snapshot taken pre-template) over
# /etc/netplan and re-apply.
set -euo pipefail

# Absolute path: a oneshot inherits only systemd's default PATH, and this
# is the one script that must not fail to find netplan on the SSH-dead path.
netplan=/usr/sbin/netplan

if [ -f /run/netplan_keep ]; then
  rm -f /run/netplan_keep
  logger -t netplan_rollback "cancelled — apply confirmed OK"
  exit 0
fi

# Refuse to touch /etc/netplan unless we have a non-empty snapshot to
# restore. A missing OR empty snapshot (tmpfs wiped, snapshot died mid-
# converge, a --start-at-task resume that skipped it, or /etc/netplan was
# empty when snapshotted) would otherwise leave the box with no netplan
# config at all -- a worse lockout than the bad config we are rolling back.
if [ ! -d /run/netplan_prev ] || [ -z "$(ls -A /run/netplan_prev 2>/dev/null)" ]; then
  logger -p user.err -t netplan_rollback "ABORT: no usable /run/netplan_prev snapshot; leaving /etc/netplan untouched"
  exit 1
fi

logger -p user.err -t netplan_rollback "auto-rollback firing: restoring previous /etc/netplan"
# Stage the restore on the same filesystem as /etc/netplan first, so the
# swap-in is two back-to-back atomic renames -- /etc/netplan is never
# absent during the slow tmpfs->rootfs copy. The presumed-broken config is
# kept aside as /etc/netplan.rollback_failed so a part-way restore stays
# recoverable from the console.
rm -rf /etc/netplan.restoring /etc/netplan.rollback_failed
mv /run/netplan_prev /etc/netplan.restoring
mv /etc/netplan /etc/netplan.rollback_failed
mv /etc/netplan.restoring /etc/netplan
if "$netplan" apply; then
  logger -t netplan_rollback "rollback apply succeeded"
else
  logger -p user.err -t netplan_rollback "rollback apply FAILED — console recovery needed"
  exit 1
fi
