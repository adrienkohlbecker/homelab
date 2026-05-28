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

systemd_timers_update_every=60
systemd_timers_priority=90100

systemd_timers_meta_dir="${systemd_timers_meta_dir:-/etc/netdata/charts.d/systemd_timers.d}"

# Netdata chart/dim ids must be alnum + underscore.
_systemd_timers_safe() {
  printf '%s' "$1" | tr -c '[:alnum:]_' '_'
}

# LastTriggerUSec prints as a human date ("Thu 2026-05-14 12:00:00 UTC")
# or "n/a" if the timer has never fired. `--timestamp=us` would give raw
# microseconds but only landed in systemd 252; jammy ships 249. LC_ALL=C
# pins the date format so `date -d` can always parse it.
_systemd_timers_last_trigger_epoch() {
  local raw
  raw=$(LC_ALL=C systemctl show --value -p LastTriggerUSec "$1.timer" 2>/dev/null)
  if [ -z "$raw" ] || [ "$raw" = "n/a" ]; then
    printf 0
    return
  fi
  date -d "$raw" +%s 2>/dev/null || printf 0
}

systemd_timers_check() {
  command -v systemctl >/dev/null 2>&1 || {
    error "systemd_timers: systemctl missing"
    return 1
  }
  # check() always succeeds so charts.d.plugin keeps the module enabled
  # even when no timers are registered yet. update() handles the empty
  # case (glob expands to no .conf files -> empty for-loop -> no output).
  # The metadata dir itself is owned by ansible (systemd_timer/install.yml).
  return 0
}

# Charts are emitted lazily from update() the first time each timer is
# observed (and re-emitted on plugin restart). Lets timers added after
# netdata starts surface without a restart. Removed timers fall out of
# the scan and netdata auto-obsoletes their charts.
systemd_timers_create() {
  return 0
}

declare -A _systemd_timers_seen

systemd_timers_update() {
  local f name period installed last age overdue safe now
  now=$(date +%s)
  for f in "${systemd_timers_meta_dir}"/*.conf; do
    [ -f "$f" ] || continue
    name=$(basename "$f" .conf)
    period=$(awk -F= '$1=="period_secs"{print $2+0; exit}' "$f")
    if [ -z "$period" ] || [ "$period" -le 0 ]; then
      error "systemd_timers: $f missing or invalid period_secs"
      continue
    fi
    safe=$(_systemd_timers_safe "$name")
    if [ -z "${_systemd_timers_seen[$name]}" ]; then
      cat <<EOF
CHART systemd_timers.${safe} '' "systemd timer freshness: ${name}" "secs" systemd_timers systemd.timer_lag area ${systemd_timers_priority} ${systemd_timers_update_every}
CLABEL timer "${name}" 1
CLABEL_COMMIT
DIMENSION age_secs '' absolute 1 1
DIMENSION overdue '' absolute 1 1
EOF
      _systemd_timers_seen[$name]=1
    fi
    installed=$(stat -c %Y "$f" 2>/dev/null || echo "$now")
    last=$(_systemd_timers_last_trigger_epoch "$name")
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
  return 0
}
