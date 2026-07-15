#!/bin/bash
set -euo pipefail
# Re-bootstrap keepalived after a UniFi OS firmware update.
# Firmware replaces the SquashFS root, wiping /usr/ — the keepalived apt
# package disappears. Systemd units in /etc/systemd/system/ (overlay upper
# dir) usually survive but may not after a major firmware update — re-deploy
# symlinks unconditionally.

DATADIR=/data/keepalived

# -- keepalived ----------------------------------------------------------
# Wrapped in a subshell so an apt failure does not abort the rest of
# the recovery (sysctl, service start).
if ! command -v keepalived &>/dev/null; then
  (
    # Suppress the package postinst from auto-starting keepalived before the
    # symlinks/sysctl below are in place (mirrors converge's
    # policy_rc_d: 101). The EXIT trap removes the shim on any subshell exit.
    printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d
    chmod +x /usr/sbin/policy-rc.d
    trap 'rm -f /usr/sbin/policy-rc.d' EXIT
    apt-get update -qq
    apt-get install -y -qq keepalived
  ) || logger -t on-boot-keepalived "WARNING: keepalived install failed, continuing with infra setup"
fi

# -- symlinks from persistent storage ------------------------------------
ln -sfn "$DATADIR/keepalived.conf" /etc/keepalived/keepalived.conf
ln -sfn "$DATADIR/healthcheck.sh" /usr/local/bin/dnsmasq_healthcheck

# -- heartbeat timer (firmware-update detection via Kuma push) ------------
ln -sfn "$DATADIR/udm_keepalived_heartbeat.service" /etc/systemd/system/udm_keepalived_heartbeat.service
ln -sfn "$DATADIR/udm_keepalived_heartbeat.timer" /etc/systemd/system/udm_keepalived_heartbeat.timer

systemctl daemon-reload

# -- sysctl ---------------------------------------------------------------
sysctl -w net.ipv4.ip_nonlocal_bind=1

# -- validate and start ---------------------------------------------------
if command -v keepalived &>/dev/null &&
  keepalived -t -f /etc/keepalived/keepalived.conf &>/dev/null; then
  systemctl enable --now keepalived.service
else
  logger -t on-boot-keepalived "WARNING: keepalived config invalid, skipping start"
fi
systemctl enable --now udm_keepalived_heartbeat.timer
