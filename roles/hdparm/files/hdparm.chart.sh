# SPDX-License-Identifier: GPL-3.0-or-later
# vim:ft=sh
# shellcheck shell=bash  # sourced by netdata charts.d.plugin, no shebang
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

# Chart creation caches Netdata's normalized id per device so the update loop
# reads pure Bash values instead of forking fixid every minute.
declare -A _hdparm_safe_ids=()

hdparm_check() {
  require_cmd hdparm || return 1
}

hdparm_create() {
  local dev safe
  for dev in "${hdparm_disks[@]}"; do
    _hdparm_safe_ids[$dev]=$(fixid "$dev")
    safe=${_hdparm_safe_ids[$dev]}
    cat <<EOF
CHART hdparm.${safe} '' "Drive power state: ${dev}" "state" hdparm hdparm.power_state line ${hdparm_priority} ${hdparm_update_every}
CLABEL device "${dev}" 1
CLABEL_COMMIT
DIMENSION active '' absolute 1 1
DIMENSION standby '' absolute 1 1
DIMENSION sleeping '' absolute 1 1
DIMENSION unknown '' absolute 1 1
DIMENSION collector_error '' absolute 1 1
EOF
  done
  return 0
}

hdparm_update() {
  local dev safe path state a s sl u ce
  for dev in "${hdparm_disks[@]}"; do
    safe=${_hdparm_safe_ids[$dev]}
    path="/dev/disk/by-id/${dev}"
    a=0
    s=0
    sl=0
    u=0
    ce=0
    # Per-device call so a single bad drive (missing by-id symlink after
    # a disk replacement, transient SATA-link drop) only collapses its
    # own chart — not every drive in the list. The previous batch form
    # short-circuited at hdparm's first per-drive failure.
    #
    # `unknown` is hdparm reporting the drive responded with a power-
    # state code outside the known set (rare; a SATA-link drift
    # symptom). `collector_error` is the monitoring stack failing to
    # read the drive at all — missing by-id symlink, hdparm exit ≠ 0,
    # empty output — operationally distinct from a drive misbehaving.
    #
    # `timeout -k 2 5` bounds a wedged-drive call: SIGTERM at 5s,
    # SIGKILL 2s later. exit 124/137 short-circuits the `&&` to the
    # `else` arm and reports collector_error like any other read
    # failure -- so one bad drive doesn't block the rest of the
    # update cycle.
    if [ -e "$path" ] && state=$(timeout -k 2 5 hdparm -C "$path" 2>/dev/null | awk '/^ drive state is:/{sub(/^ drive state is:  /,""); print; exit}'); then
      case "$state" in
      "active/idle") a=1 ;;
      "standby") s=1 ;;
      "sleeping") sl=1 ;;
      "unknown") u=1 ;;
      *) ce=1 ;;
      esac
    else
      ce=1
    fi
    cat <<EOF
BEGIN hdparm.${safe} ${1}
SET active = $a
SET standby = $s
SET sleeping = $sl
SET unknown = $u
SET collector_error = $ce
END
EOF
  done
  return 0
}
