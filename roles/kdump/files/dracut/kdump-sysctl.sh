#!/bin/sh
# shellcheck shell=dash
# pre-pivot hook (dracut port of kdump-tools' initramfs.local-bottom): on a
# capture boot, stage the kdump sysctl overrides into /run/sysctl.d. /run
# survives switch-root, so the real-root systemd-sysctl applies the drop-in.

# shellcheck source=/dev/null
type getarg >/dev/null 2>&1 || . /lib/dracut-lib.sh

kdump_stage_sysctls() {
  local conf="/etc/kdump/sysctl.conf"
  local newroot="${NEWROOT:-/sysroot}"
  local sysd="sysctl.d/"
  local last_conf fname

  # Only act during an actual crash-capture boot.
  [ -e /proc/vmcore ] || return 0

  # Nothing to stage without the bundled source.
  if [ ! -f "$conf" ]; then
    warn "kdump-tools: $conf missing from initrd; skipping sysctl hand-off"
    return 0
  fi

  # /run is the bridge across switch-root.
  if [ ! -d /run ]; then
    warn "kdump-tools: /run unavailable; cannot stage kdump sysctls"
    return 0
  fi

  # Name the drop-in after the last-sorting *.conf in the real root, so the
  # kdump overrides are applied last and win.
  last_conf="$(find "${newroot}/usr/lib/${sysd}" "${newroot}/usr/local/lib/${sysd}" \
    "${newroot}/lib/${sysd}" "${newroot}/etc/${sysd}" "${newroot}/run/${sysd}" \
    -maxdepth 1 -name '*.conf' 2>/dev/null | sed 's|.*/||' | LC_ALL=C sort | tail -n 1)"

  fname="${last_conf:-zz}-kdump.conf"

  mkdir -p /run/sysctl.d
  if cp -p "$conf" "/run/sysctl.d/${fname}"; then
    info "kdump-tools: staged sysctl overrides as /run/sysctl.d/${fname}"
  else
    warn "kdump-tools: failed to stage sysctl overrides into /run/sysctl.d"
  fi
}

kdump_stage_sysctls
unset -f kdump_stage_sysctls

return 0
