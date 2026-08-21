#!/bin/bash
# The systemd condition skips this script after a successful SSH canary;
# reaching it means the pre-template snapshot must be restored.
set -euo pipefail

# Never replace /etc/netplan with a missing or empty snapshot; that would turn
# the original failure into a complete network lockout.
if [ ! -d /run/netplan_prev ] || [ -z "$(ls -A /run/netplan_prev 2>/dev/null)" ]; then
  logger -p user.err -t netplan_rollback "ABORT: no usable /run/netplan_prev snapshot; leaving /etc/netplan untouched"
  exit 1
fi

logger -p user.err -t netplan_rollback "auto-rollback firing: restoring previous /etc/netplan"
# Stage the slow copy beside /etc/netplan, then swap with atomic renames. Keep
# the source snapshot reusable and the broken config until recovery succeeds.
rm -rf /etc/netplan.restoring /etc/netplan.rollback_failed
cp -a /run/netplan_prev /etc/netplan.restoring
mv /etc/netplan /etc/netplan.rollback_failed
mv /etc/netplan.restoring /etc/netplan
# Use an absolute path on the SSH-dead path rather than systemd's default PATH.
if /usr/sbin/netplan apply; then
  rm -rf /etc/netplan.rollback_failed
  logger -t netplan_rollback "rollback apply succeeded"
else
  logger -p user.err -t netplan_rollback "rollback apply FAILED — console recovery needed"
  exit 1
fi
