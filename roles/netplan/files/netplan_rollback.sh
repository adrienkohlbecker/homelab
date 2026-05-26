#!/bin/bash
# Netplan auto-rollback script invoked by netplan_rollback.service.
# The corresponding .timer fires ~90s after a safe-apply arms it.
# If the apply succeeded, ansible touches /run/netplan_keep before
# the timer fires; we then exit without rolling back. Otherwise we
# restore /run/netplan_prev (snapshot taken pre-template) over
# /etc/netplan and re-apply.
set -e
if [ -f /run/netplan_keep ]; then
  rm -f /run/netplan_keep
  logger -t netplan_rollback "cancelled — apply confirmed OK"
  exit 0
fi
logger -t netplan_rollback "auto-rollback firing: restoring previous /etc/netplan"
rm -rf /etc/netplan
mv /run/netplan_prev /etc/netplan
netplan apply
