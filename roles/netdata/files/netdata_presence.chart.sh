# SPDX-License-Identifier: GPL-3.0-or-later
# vim:ft=sh
# shellcheck shell=bash  # sourced by netdata charts.d.plugin, no shebang
#
# Netdata charts.d collector: contexts + collectors registered with the
# local netdata instance.
#
# Native $last_collected_t templates only bind to charts that already
# exist; a template with `on: <missing-chart>` silently fails to
# instantiate. The gap-closer here lets us alert on "configured chart
# never appeared in netdata's chart registry" (renamed collector job,
# disabled plugin, upstream context rename, etc.).
#
# Self-reference: this collector runs *inside* netdata. If netdata is
# wedged hard enough that the local HTTP API doesn't answer, charts.d
# won't run either -- but in that case netdata.service has already
# failed and the systemd_service_unit_failed_state alert fires. If
# netdata is up but a specific go.d job didn't register, the API
# answers and we still surface the missing entry.
#
# Per-host config comes from /etc/netdata/charts.d/netdata_presence.conf:
#   netdata_presence_contexts=( "name=<context_id>" ... )
#   netdata_presence_collectors=( "<go.d:collector:plugin:job>" ... )
# Bash 4 lacks portable associative-array export through `source`, so
# contexts use a flat "name=id" array that we split on the first '='.
# (A context id legitimately can contain '.', '_', or ':', so split
# at the *first* '=' only.)

netdata_presence_update_every=60
netdata_presence_priority=90200

netdata_presence_contexts=()
netdata_presence_collectors=()

netdata_presence_api="${netdata_presence_api:-http://127.0.0.1:19999}"
# curl is in our `roles/user` baseline package list; jq likewise. If
# either is missing on a host we don't ship to, _check fails loud.

_netdata_presence_safe() {
  printf '%s' "$1" | tr -c '[:alnum:]_' '_'
}

netdata_presence_check() {
  command -v curl >/dev/null 2>&1 || {
    error "netdata_presence: curl missing"
    return 1
  }
  command -v jq >/dev/null 2>&1 || {
    error "netdata_presence: jq missing"
    return 1
  }
  # check() always succeeds even when the lists are empty: a host can
  # legitimately ship without configured contexts/collectors (test fixtures,
  # a minimal target). update() handles the empty case (no-op loops).
  return 0
}

netdata_presence_create() {
  return 0
}

declare -A _netdata_presence_seen_contexts
declare -A _netdata_presence_seen_collectors

# /api/v2/contexts returns {"contexts":{"<id>":{...},...}}; one-liner
# pulls just the key set. --fail makes curl emit nothing and exit non-zero
# on an HTTP 4xx/5xx (e.g. a 503 mid-reload), so an error body never reaches
# jq as if it were data; --max-time bounds a wedged netdata API. On any
# failure the output is empty, so update()'s empty-guard skips the cycle
# rather than emitting fake "missing" values.
_netdata_presence_fetch_contexts() {
  curl -sS --fail --max-time 3 "${netdata_presence_api}/api/v2/contexts" 2>/dev/null |
    jq -r '.contexts | keys[]' 2>/dev/null
}

_netdata_presence_fetch_charts() {
  curl -sS --fail --max-time 3 "${netdata_presence_api}/api/v1/charts" 2>/dev/null |
    jq -r '.charts | keys[]' 2>/dev/null
}

# go.d:collector:<plugin>:<job> -> the chart-id prefix that job's charts
# carry. We detect collector presence by "did this collector emit any
# chart at all", matched by that prefix. netdata 2.x also exposes a per-job
# `netdata.plugin_data_collection_status` chart, but that only appears once
# a job has run, so it can't see a collector that never instantiated any
# chart -- which is exactly the gap this prefix check closes. Alerting on
# that 2.x chart is a tracked follow-up (see context_liveness.conf.j2).
#
# Naming rule (stable across 1.46 and 2.x): the chart-id is
# "<plugin>.<context>" when plugin==job (e.g. zfspool, nvme, fail2ban),
# otherwise "<plugin>_<job>.<context>" (e.g. chrony_local, x509check_host_cert),
# and a per-instance job fans out to "<plugin>_<job>_<instance>.<context>".
#
# Emits "<mode> <prefix>": mode 'exact' (plugin==job) means only
# "<prefix>.<ctx>" charts count -- a "<prefix>_<otherjob>.*" chart belongs to
# a *different* job and must not satisfy this entry. mode 'instanced'
# (plugin!=job) also accepts "<prefix>_<instance>.<ctx>" fan-out charts.
# Empty for non-go.d ids.
_netdata_presence_collector_chart_prefix() {
  local cid=$1
  local IFS=:
  read -r p1 p2 plugin job _rest <<<"$cid"
  if [ "$p1" != "go.d" ] || [ "$p2" != "collector" ] || [ -z "$plugin" ] || [ -z "$job" ]; then
    printf ''
    return
  fi
  if [ "$plugin" = "$job" ]; then
    printf 'exact %s' "$plugin"
  else
    printf 'instanced %s_%s' "$plugin" "$job"
  fi
}

netdata_presence_update() {
  local entry name ctx_id present safe expected line _chart_id _mode
  local -A api_contexts=()
  local -A api_charts=()

  if [ "${#netdata_presence_contexts[@]}" -gt 0 ]; then
    while IFS= read -r line; do
      [ -n "$line" ] && api_contexts[$line]=1
    done < <(_netdata_presence_fetch_contexts)
    # Empty result + non-empty config -> netdata API hiccup; skip this
    # cycle. The chart's last_collected_t goes stale and the standard
    # chart-staleness alarm catches it.
    [ "${#api_contexts[@]}" -gt 0 ] || return 0
  fi

  if [ "${#netdata_presence_collectors[@]}" -gt 0 ]; then
    while IFS= read -r line; do
      [ -n "$line" ] && api_charts[$line]=1
    done < <(_netdata_presence_fetch_charts)
    [ "${#api_charts[@]}" -gt 0 ] || return 0
  fi

  for entry in "${netdata_presence_contexts[@]}"; do
    name=${entry%%=*}
    ctx_id=${entry#*=}
    if [ "$name" = "$entry" ] || [ -z "$name" ] || [ -z "$ctx_id" ]; then
      error "netdata_presence: malformed context entry '$entry' (expected name=context_id)"
      continue
    fi
    safe=$(_netdata_presence_safe "$name")
    present=0
    [ -n "${api_contexts[$ctx_id]:-}" ] && present=1
    if [ -z "${_netdata_presence_seen_contexts[$name]:-}" ]; then
      cat <<EOF
CHART netdata_presence.context_${safe} '' "Netdata context presence: ${name}" "present" netdata_presence netdata_presence.context line ${netdata_presence_priority} ${netdata_presence_update_every}
CLABEL context "${name}" 1
CLABEL context_id "${ctx_id}" 1
CLABEL_COMMIT
DIMENSION present '' absolute 1 1
EOF
      _netdata_presence_seen_contexts[$name]=1
    fi
    cat <<EOF
BEGIN netdata_presence.context_${safe} ${1}
SET present = $present
END
EOF
  done

  for entry in "${netdata_presence_collectors[@]}"; do
    [ -n "$entry" ] || continue
    read -r _mode expected <<<"$(_netdata_presence_collector_chart_prefix "$entry")"
    safe=$(_netdata_presence_safe "$entry")
    present=0
    if [ -n "$expected" ]; then
      # Walk the chart keyset (bash 4 has no native "any key matches glob"):
      # a "<prefix>.<ctx>" chart always counts; a "<prefix>_<instance>.<ctx>"
      # fan-out chart (e.g. upsd_local_eaton.battery_charge_percentage, where
      # `eaton` is the UPS name) counts only in 'instanced' mode -- in 'exact'
      # mode an underscore after the prefix means a *sibling* job, not ours.
      # ~6k charts on lab takes O(ms) at the 60s update cadence.
      for _chart_id in "${!api_charts[@]}"; do
        case "$_chart_id" in
        "$expected".*)
          present=1
          break
          ;;
        "$expected"_*)
          if [ "$_mode" = instanced ]; then
            present=1
            break
          fi
          ;;
        esac
      done
    fi
    if [ -z "${_netdata_presence_seen_collectors[$entry]:-}" ]; then
      cat <<EOF
CHART netdata_presence.collector_${safe} '' "Netdata collector presence: ${entry}" "present" netdata_presence netdata_presence.collector line $((netdata_presence_priority + 1)) ${netdata_presence_update_every}
CLABEL collector "${entry}" 1
CLABEL_COMMIT
DIMENSION present '' absolute 1 1
EOF
      _netdata_presence_seen_collectors[$entry]=1
    fi
    cat <<EOF
BEGIN netdata_presence.collector_${safe} ${1}
SET present = $present
END
EOF
  done

  return 0
}
