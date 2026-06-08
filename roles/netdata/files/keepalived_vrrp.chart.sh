# SPDX-License-Identifier: GPL-3.0-or-later
# vim:ft=sh
# shellcheck shell=bash  # sourced by netdata charts.d.plugin, no shebang
#
# Netdata charts.d collector: keepalived VRRP state, read directly off the
# host rather than from an exporter. Two signals on one chart:
#   holds_vip  1 when the VRRP virtual IP is bound to a local interface
#              (i.e. this host is currently MASTER), else 0. The VIP rides a
#              vmac child (vrrp.<vrid>) on the MASTER and is absent on a BACKUP.
#   active     1 when keepalived.service is running, else 0.
# Replaces the retired keepalived_exporter as the "is this host MASTER" source;
# the keepalived_lost_master health alarm (primary peer only) fires on a
# sustained holds_vip=0.
#
# Per-host config (/etc/netdata/charts.d/keepalived_vrrp.conf):
#   keepalived_vrrp_vip="10.x.y.z"   # the VRRP virtual IP to look for

keepalived_vrrp_update_every=5
keepalived_vrrp_priority=90100
keepalived_vrrp_vip="${keepalived_vrrp_vip:-}"

keepalived_vrrp_check() {
  # Disable cleanly (charts.d skips the module) where this host isn't a VRRP
  # peer — no VIP configured — or `ip` is somehow missing.
  [ -n "$keepalived_vrrp_vip" ] || {
    error "keepalived_vrrp: keepalived_vrrp_vip unset"
    return 1
  }
  command -v ip >/dev/null 2>&1 || {
    error "keepalived_vrrp: ip command missing"
    return 1
  }
  return 0
}

keepalived_vrrp_create() {
  cat <<EOF
CHART keepalived_vrrp.state '' "keepalived VRRP master state" "state" keepalived keepalived_vrrp.state line ${keepalived_vrrp_priority} ${keepalived_vrrp_update_every}
CLABEL vip "${keepalived_vrrp_vip}" 1
CLABEL_COMMIT
DIMENSION holds_vip '' absolute 1 1
DIMENSION active 'keepalived running' absolute 1 1
EOF
  return 0
}

keepalived_vrrp_update() {
  local held=0 active=0
  # `ip -o -4 addr show` is one address per line; field 4 is "<ip>/<prefix>".
  # Match the VIP as an exact, whole-line fixed string so 10.1.1.2 can't
  # match 10.1.1.20. No pipe-to-grep with early exit (SIGPIPE under set -e);
  # awk does the compare itself.
  if ip -o -4 addr show 2>/dev/null |
    awk -v vip="$keepalived_vrrp_vip" '{ split($4, a, "/"); if (a[1] == vip) found=1 } END { exit !found }'; then
    held=1
  fi
  systemctl is-active --quiet keepalived.service && active=1
  cat <<EOF
BEGIN keepalived_vrrp.state ${1}
SET holds_vip = ${held}
SET active = ${active}
END
EOF
  return 0
}
