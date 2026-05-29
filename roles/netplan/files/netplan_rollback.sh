#!/bin/bash
# Netplan auto-rollback script invoked by netplan_rollback.service.
# The corresponding .timer fires ~90s after a safe-apply arms it. If the
# apply succeeded, the apply path touches /run/netplan_keep and the unit's
# ConditionPathExists=!/run/netplan_keep skips this script entirely;
# reaching here means the marker is absent, so restore /run/netplan_prev
# (snapshot taken pre-template) over /etc/netplan and re-apply.
set -euo pipefail

# Absolute path: a oneshot inherits only systemd's default PATH, and this
# is the one script that must not fail to find netplan on the SSH-dead path.
netplan=/usr/sbin/netplan

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
# Copy the snapshot in first (the slow tmpfs->rootfs step), staged on the
# same filesystem as /etc/netplan so the swap-in is then two atomic renames.
# /etc/netplan is briefly absent only between those two renames, never
# during the slow copy. cp -a rather than mv leaves /run/netplan_prev intact
# so a re-fire (or the next converge) still has a snapshot to work from. The
# presumed-broken config is kept aside as /etc/netplan.rollback_failed so a
# part-way restore stays recoverable from the console.
rm -rf /etc/netplan.restoring /etc/netplan.rollback_failed
cp -a /run/netplan_prev /etc/netplan.restoring
mv /etc/netplan /etc/netplan.rollback_failed
mv /etc/netplan.restoring /etc/netplan
if "$netplan" apply; then
  logger -t netplan_rollback "rollback apply succeeded"
else
  logger -p user.err -t netplan_rollback "rollback apply FAILED — console recovery needed"
  exit 1
fi
