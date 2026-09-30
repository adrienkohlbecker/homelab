#!/bin/bash
# Dracut supplies initdir and moddir when sourcing this module.
# shellcheck disable=SC2154
# 50kdump-tools - thin, opt-in kdump capture-initrd module for kdump-tools.
#
# Crash capture runs after switch-root in the real root via
# kdump-tools-dump.service. This module applies hugepage sysctl overrides in
# the initramfs and stages them across switch-root (the dracut port of
# initramfs-tools' initramfs.hook + initramfs.local-bottom). check() returns 255
# so it is never auto-included; the kdump-tools preset force-adds it.

KDUMP_SYSCTL_CONF="/etc/kdump/sysctl.conf"

check() {
  # Opt-in: selected by the kdump-tools preset, never auto-included.
  return 255
}

install() {
  local last_conf
  # The staging hook uses these; only sed is guaranteed by 80base.
  inst_multiple find sed sort tail

  # Bundle the sysctl source for the runtime hook.
  if [[ -f "${dracutsysrootdir-}${KDUMP_SYSCTL_CONF}" ]]; then
    inst_simple "${KDUMP_SYSCTL_CONF}"
    # systemd-sysctl merges these before applying any host hugepage values.
    last_conf="$(find "$initdir/usr/lib/sysctl.d" "$initdir/usr/local/lib/sysctl.d" \
      "$initdir/lib/sysctl.d" "$initdir/etc/sysctl.d" "$initdir/run/sysctl.d" \
      -maxdepth 1 -name '*.conf' 2>/dev/null | sed 's|.*/||' | LC_ALL=C sort | tail -n 1)"
    inst_simple "${KDUMP_SYSCTL_CONF}" "/etc/sysctl.d/${last_conf:-zz}-kdump.conf"
  else
    dwarn "kdump-tools: ${KDUMP_SYSCTL_CONF} not found; sysctl overrides will not be staged"
  fi

  # Hook that stages the overrides into /run/sysctl.d before switch-root.
  inst_hook pre-pivot 99 "$moddir/kdump-sysctl.sh"
}
