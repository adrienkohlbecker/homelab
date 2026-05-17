# SPDX-License-Identifier: GPL-3.0-or-later
# vim:ft=sh
#
# Netdata charts.d collector: ATA drive power state via `hdparm -C`.
#
# Emits one chart per configured device with four bool dimensions
# (active/standby/sleeping/unknown). The chart context is shared
# (`hdparm.power_state`), so a single health template fans out across
# every drive without per-device enumeration in alerts.conf.
#
# Drive list comes from /etc/netdata/charts.d/hdparm.conf as the
# bash array `hdparm_disks=(...)`; entries are /dev/disk/by-id stems.
#
# `hdparm -C` is non-destructive and explicitly does NOT spin a drive
# up to read its power mode, so polling it every minute does not
# defeat hd_idle's spindown.

hdparm_update_every=60
hdparm_priority=90000

hdparm_disks=()

# Netdata dim/chart ids must be alnum + underscore.
_hdparm_safe() {
  printf '%s' "$1" | tr -c '[:alnum:]_' '_'
}

hdparm_check() {
  command -v hdparm >/dev/null 2>&1 || {
    error "hdparm: 'hdparm' binary missing"
    return 1
  }
  [ "${#hdparm_disks[@]}" -gt 0 ] || {
    # Quietly skip rather than error -- hosts with no rotational disks
    # legitimately ship an empty list (box, mac dev VMs).
    return 1
  }
  return 0
}

hdparm_create() {
  local dev safe
  for dev in "${hdparm_disks[@]}"; do
    safe=$(_hdparm_safe "$dev")
    cat <<EOF
CHART hdparm.${safe} '' "Drive power state: ${dev}" "state" hdparm hdparm.power_state line ${hdparm_priority} ${hdparm_update_every}
CLABEL device "${dev}" 1
CLABEL_COMMIT
DIMENSION active '' absolute 1 1
DIMENSION standby '' absolute 1 1
DIMENSION sleeping '' absolute 1 1
DIMENSION unknown '' absolute 1 1
EOF
  done
  return 0
}

hdparm_update() {
  local dev safe path state a s sl u
  for dev in "${hdparm_disks[@]}"; do
    safe=$(_hdparm_safe "$dev")
    path="/dev/disk/by-id/${dev}"
    a=0; s=0; sl=0; u=0
    # Per-device call so a single bad drive (missing by-id symlink after
    # a disk replacement, transient SATA-link drop) only collapses its
    # own chart to unknown=1 — not every drive in the list. The previous
    # batch form short-circuited at hdparm's first per-drive failure.
    if [ -e "$path" ] && state=$(hdparm -C "$path" 2>/dev/null | awk '/^ drive state is:/{sub(/^ drive state is:  /,""); print; exit}'); then
      case "$state" in
        "active/idle") a=1 ;;
        "standby")     s=1 ;;
        "sleeping")    sl=1 ;;
        *)             u=1 ;;
      esac
    else
      u=1
    fi
    cat <<EOF
BEGIN hdparm.${safe} ${1}
SET active = $a
SET standby = $s
SET sleeping = $sl
SET unknown = $u
END
EOF
  done
  return 0
}
