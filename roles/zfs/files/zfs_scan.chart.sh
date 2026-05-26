# SPDX-License-Identifier: GPL-3.0-or-later
# vim:ft=sh
# shellcheck shell=bash  # sourced by netdata charts.d.plugin, no shebang
#
# Netdata charts.d collector: ZFS scrub/resilver in-progress state.
#
# Emits a single bool dimension (`active`): 1 if any pool on the host
# is currently scrubbing or resilvering, 0 otherwise. Used by
# roles/hdparm/files/hdparm_alerts.conf to mute hdparm_drive_active
# during scrubs (where every member drive spins for hours by design)
# without losing coverage outside the scrub window.
#
# `zpool status` is read-only and runnable by the netdata user on
# Ubuntu noble/jammy: openzfs ships /lib/udev/rules.d/90-zfs.rules
# with `MODE="0666"` on /dev/zfs, so libzfs's ZFS_IOC_POOL_STATS
# ioctl works unprivileged. If a host ever tightens /dev/zfs perms,
# the check function will catch it via the trial `zpool status` call.

zfs_scan_update_every=60
zfs_scan_priority=90100

zfs_scan_check() {
  command -v zpool >/dev/null 2>&1 || {
    error "zfs_scan: 'zpool' binary missing"
    return 1
  }
  # Probe once so a permission tightening on /dev/zfs (or missing kmod)
  # surfaces here (collector disabled, visible in netdata log) rather
  # than as silent-zero readings. `zpool status` exits 0 with "no pools
  # available" when nothing is imported — that's fine, the chart stays
  # at active=0 forever, which is the truth.
  zpool status >/dev/null 2>&1 || {
    error "zfs_scan: 'zpool status' failed (permission denied or kmod missing)"
    return 1
  }
  return 0
}

zfs_scan_create() {
  cat <<EOF
CHART zfs_scan.any_in_progress '' "ZFS scrub or resilver in progress" "bool" zfs_scan zfs_scan.any_in_progress line ${zfs_scan_priority} ${zfs_scan_update_every}
DIMENSION active '' absolute 1 1
EOF
  return 0
}

zfs_scan_update() {
  local active=0
  if zpool status 2>/dev/null | grep -qE 'scrub in progress|resilver in progress'; then
    active=1
  fi
  cat <<EOF
BEGIN zfs_scan.any_in_progress ${1}
SET active = $active
END
EOF
  return 0
}
