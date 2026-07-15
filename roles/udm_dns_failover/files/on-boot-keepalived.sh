#!/bin/bash
set -euo pipefail
# Re-bootstrap keepalived after a UniFi OS firmware update.
# Firmware replaces the SquashFS root, wiping /usr/ — the keepalived apt
# package disappears. Systemd units in /etc/systemd/system/ (overlay upper
# dir) usually survive but may not after a major firmware update — re-deploy
# symlinks unconditionally.

DATADIR=/data/keepalived

# -- keepalived ----------------------------------------------------------
# Wrapped in a subshell so a dpkg/apt failure does not abort the rest of
# the recovery (sysctl, service start).
if ! command -v keepalived &>/dev/null; then
  (
    # Suppress the package postinst from auto-starting keepalived before the
    # symlinks/sysctl below are in place (mirrors converge's
    # policy_rc_d: 101). The EXIT trap removes the shim on any subshell exit.
    printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d
    chmod +x /usr/sbin/policy-rc.d
    trap 'rm -f /usr/sbin/policy-rc.d' EXIT
    shopt -s nullglob
    cached_debs=("$DATADIR"/cache/keepalived_*.deb)
    if ((${#cached_debs[@]})); then
      latest="$(printf '%s\n' "${cached_debs[@]}" | sort -V | tail -1)"
      # A firmware update can move libc/libssl, so dpkg -i may exit 0 while
      # leaving keepalived unrunnable (unmet deps). Verify the binary actually
      # runs; otherwise fall through to apt, which pulls the updated deps.
      dpkg -i "$latest" && keepalived -v &>/dev/null || { apt-get update -qq && apt-get install -y -qq keepalived; }
    else
      apt-get update -qq
      apt-get install -y -qq keepalived
      mkdir -p "$DATADIR/cache"
      chmod 700 "$DATADIR/cache"
      cp /var/cache/apt/archives/keepalived_*.deb "$DATADIR/cache/" 2>/dev/null || logger -t on-boot-keepalived "WARNING: failed to re-seed the keepalived .deb cache"
    fi
  ) || logger -t on-boot-keepalived "WARNING: keepalived install failed, continuing with infra setup"
fi

# -- symlinks from persistent storage ------------------------------------
ln -sfn "$DATADIR/keepalived.conf" /etc/keepalived/keepalived.conf
mkdir -p /etc/keepalived/conf.d
ln -sfn "$DATADIR/dns.conf" /etc/keepalived/conf.d/dns.conf
ln -sfn "$DATADIR/healthcheck.sh" /usr/local/bin/dnsmasq_healthcheck

# -- heartbeat timer (firmware-update detection via Kuma push) ------------
ln -sfn "$DATADIR/udm_keepalived_heartbeat.service" /etc/systemd/system/udm_keepalived_heartbeat.service
ln -sfn "$DATADIR/udm_keepalived_heartbeat.timer" /etc/systemd/system/udm_keepalived_heartbeat.timer

# -- keepalived validate drop-in (ExecStartPre/ExecReload keepalived -t) ---
mkdir -p /etc/systemd/system/keepalived.service.d
ln -sfn "$DATADIR/override.conf" /etc/systemd/system/keepalived.service.d/override.conf
systemctl daemon-reload

# -- sysctl ---------------------------------------------------------------
sysctl -w net.ipv4.ip_nonlocal_bind=1

# -- validate and start ---------------------------------------------------
# keepalived -t passes even when conf.d/dns.conf is a dangling or
# instance-less fragment (zero vrrp_instances) -- the daemon would then run
# healthy and pointless, never claiming the VIP, which is exactly the
# post-firmware-update failure this recovery path must not leave behind.
# Also require the instance to be present, mirroring the converge assert.
if command -v keepalived &>/dev/null &&
  keepalived -t -f /etc/keepalived/keepalived.conf &>/dev/null &&
  grep -q '^vrrp_instance dns' /etc/keepalived/conf.d/dns.conf 2>/dev/null; then
  systemctl enable --now keepalived.service
else
  logger -t on-boot-keepalived "WARNING: keepalived config invalid or VRRP instance missing, skipping start"
fi
systemctl enable --now udm_keepalived_heartbeat.timer
