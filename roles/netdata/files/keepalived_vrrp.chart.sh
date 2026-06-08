# SPDX-License-Identifier: GPL-3.0-or-later
# vim:ft=sh
# shellcheck shell=bash  # sourced by netdata charts.d.plugin, no shebang
#
# Netdata charts.d collector: keepalived VRRP state, read directly off the
# host. The chart reports whether the VRRP virtual IP is bound to a local
# interface:
#   holds_vip  1 when the VRRP virtual IP is bound to a local interface
#              (i.e. this host is currently MASTER), else 0. The VIP rides a
#              vmac child (vrrp.<vrid>) on the MASTER and is absent on a BACKUP.
#
# Per-host config (/etc/netdata/charts.d/keepalived_vrrp.conf):
#   keepalived_vrrp_vip="10.x.y.z"   # the VRRP virtual IP to look for

keepalived_vrrp_update_every=30
keepalived_vrrp_priority=90100
keepalived_vrrp_vip="${keepalived_vrrp_vip:-}"

keepalived_vrrp_check() {
  # Disable cleanly (charts.d skips the module) where this host isn't a VRRP
  # peer, no VIP is configured, or `ip` is somehow missing.
  [ -n "$keepalived_vrrp_vip" ] || {
    error "keepalived_vrrp: keepalived_vrrp_vip unset"
    return 1
  }
  require_cmd ip || return 1
  return 0
}

keepalived_vrrp_create() {
  cat <<EOF
CHART keepalived_vrrp.state '' "keepalived VRRP master state" "state" keepalived keepalived_vrrp.state line ${keepalived_vrrp_priority} ${keepalived_vrrp_update_every}
CLABEL vip "${keepalived_vrrp_vip}" 1
CLABEL_COMMIT
DIMENSION holds_vip '' absolute 1 1
EOF
  return 0
}

keepalived_vrrp_update() {
  local held=0
  if ip -o -4 addr show up to "$keepalived_vrrp_vip" 2>/dev/null |
    awk 'NF { found=1 } END { exit !found }'; then
    held=1
  fi
  cat <<EOF
BEGIN keepalived_vrrp.state ${1}
SET holds_vip = ${held}
END
EOF
  return 0
}
