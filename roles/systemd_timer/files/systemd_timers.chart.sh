# SPDX-License-Identifier: GPL-3.0-or-later
# vim:ft=sh
# shellcheck shell=bash  # sourced by netdata charts.d.plugin, no shebang
#
# Netdata charts.d collector: systemd timer freshness.
#
# Emits one chart per registered timer carrying two gauges:
#   age_secs  -- seconds since last trigger (or seconds since install if
#                the timer has never fired)
#   overdue   -- 1 if age_secs exceeds 2 * period_secs, else 0
#
# Pairs with netdata's systemd_service_unit_failed_state alert: between
# the two, "ran and failed" (unit state) and "never ran / ran too long
# ago" (overdue) are both covered for every timer registered with the
# systemd_timer role.
#
# Per-timer metadata comes from /etc/netdata/charts.d/systemd_timers.d/<name>.conf;
# each file declares period_secs=<n> and is written by
# roles/systemd_timer/tasks/install.yml at install time. The file's
# mtime doubles as "installed_at" so a freshly-installed timer doesn't
# alarm before its first scheduled fire.

systemd_timers_priority=90100

# charts.d.plugin sources charts_d_systemd_timers.conf before invoking this file.
: "${systemd_timers_update_every:?}"
: "${systemd_timers_meta_dir:?}"

# Last-fire time = the mtime of the stamp file at
# /var/lib/systemd/timers/stamp-<name>.timer. System-scope monitored timers
# touch it in ExecStartPre (service.j2), so age_secs tracks attempted starts;
# failed runs are reported by the unit-failed alert instead of overdue.
# Reading the stamp beats
# `systemctl show LastTriggerUSec`: the stamp survives both reboots and unit
# reloads, whereas LastTriggerUSec blanks to n/a whenever the .timer is
# re-enabled -- which every converge that rewrites the unit does -- and would
# then read as "never fired". A missing stamp falls back to install time. The
# collector needs no systemctl and can't be tripped by a transient D-Bus hiccup.

systemd_timers_check() {
  # Stay enabled while no timers are registered; update() handles an empty dir.
  return 0
}

# Charts are emitted lazily from update() the first time each timer is
# observed (and re-emitted on plugin restart). Lets timers added after
# netdata starts surface without a restart. Removed timers are obsoleted
# explicitly by update()'s obsoletion sweep so their chart and its bound
# overdue alert are reaped promptly, rather than lingering until netdata's
# auto-stale timeout.
systemd_timers_create() {
  return 0
}

# _systemd_timers_seen maps name → safe-name; populated on first observation.
# Per-process state (sourced once by charts.d.plugin); safe-name is cached
# so fixid runs only once per timer lifetime.
declare -A _systemd_timers_seen

systemd_timers_update() {
  local f name period installed last age overdue safe now
  declare -A present
  # $EPOCHSECONDS is a bash 5.0+ built-in; noble ships bash 5.2.
  now=$EPOCHSECONDS
  for f in "${systemd_timers_meta_dir}"/*.conf; do
    [ -f "$f" ] || continue
    # Pure-bash basename equivalent -- no subprocess.
    name="${f##*/}"
    name="${name%.conf}"
    period=$(awk -F= '$1=="period_secs"{print $2+0; exit}' "$f")
    if [ -z "$period" ] || [ "$period" -le 0 ]; then
      error "systemd_timers: $f missing or invalid period_secs"
      continue
    fi
    present[$name]=1
    if [ -z "${_systemd_timers_seen[$name]:-}" ]; then
      # Compute and cache safe name on first observation only.
      safe=$(fixid "$name")
      _systemd_timers_seen[$name]="$safe"
      cat <<EOF
CHART systemd_timers.${safe} '' "systemd timer freshness: ${name}" "secs" systemd_timers systemd.timer_lag area ${systemd_timers_priority} ${systemd_timers_update_every}
CLABEL timer "${name}" 1
CLABEL_COMMIT
DIMENSION age_secs '' absolute 1 1
DIMENSION overdue '' absolute 1 1
EOF
    fi
    safe="${_systemd_timers_seen[$name]}"
    installed=$(stat -c %Y "$f" 2>/dev/null || echo "$now")
    last=$(stat -c %Y "/var/lib/systemd/timers/stamp-${name}.timer" 2>/dev/null || printf 0)
    if [ "$last" -gt 0 ]; then
      age=$((now - last))
    else
      # Never fired: count from install so the freshness chart shows a
      # growing line until the first fire, with overdue=1 only once the
      # grace window (2 * period) has elapsed without a single fire.
      age=$((now - installed))
    fi
    if [ "$age" -gt $((period * 2)) ]; then
      overdue=1
    else
      overdue=0
    fi
    cat <<EOF
BEGIN systemd_timers.${safe} ${1}
SET age_secs = $age
SET overdue = $overdue
END
EOF
  done
  # Obsoletion sweep: a timer we charted before whose metadata file has since
  # vanished (role `remove`) gets its CHART re-emitted with the `obsolete`
  # option, so netdata hides and reaps both the chart and its bound overdue
  # alert promptly instead of waiting for the auto-stale timeout. Iterating
  # the key snapshot makes the in-loop unset safe.
  for name in "${!_systemd_timers_seen[@]}"; do
    [ -n "${present[$name]:-}" ] && continue
    safe="${_systemd_timers_seen[$name]}"
    cat <<EOF
CHART systemd_timers.${safe} '' "systemd timer freshness: ${name}" "secs" systemd_timers systemd.timer_lag area ${systemd_timers_priority} ${systemd_timers_update_every} obsolete
EOF
    unset '_systemd_timers_seen[$name]'
  done
  return 0
}
