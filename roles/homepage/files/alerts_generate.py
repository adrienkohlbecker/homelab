"""Generate the homepage alerts panel's static HTML snapshot.

Polls each configured host's `/api/v1/alarms?active` netdata endpoint in
parallel and writes index.html — a dark-themed, iframe-friendly alert list.

Run as a Type=oneshot systemd service fired by homepage_alerts.timer on
a sub-minute cadence (OnCalendar=*:*:0/30 + AccuracySec=1s on the
homepage host). nginx serves the file directly from OUTPUT_DIR; no
proxy_pass, no long-running process.

Inputs (env):
- NETDATA_HOSTS: comma-separated list of `<name>=<query-url>[=<click-url>]`
  triples. The query-url is fetched server-side, the click-url is the
  public netdata vhost a browser can navigate to. If only one URL is
  given, both are set to it.
  Example: "lab=http://localhost:19999=https://netdata.lab.fahm.fr,pug=https://netdata.pug.fahm.fr"
- OUTPUT_DIR: directory to write the files into (default
  /var/www/homepage_alerts). Must be writable by the unit's User=.
- NETDATA_ALERTS_TOKEN: optional. When set, every request to a
  non-loopback query-url gets `?auth_token=<token>` (or `&auth_token=`
  if the path already carries a query string) so it bypasses the
  netdata vhost's Authelia forward-auth gate (see
  roles/nginx/tasks/site.yml's bypass_query_param). Loopback URLs
  (localhost / 127.0.0.1 / ::1) skip the token — they bypass nginx
  entirely.

Embedded in the homepage dashboard via an iframe widget mounted at
/alerts/ on the homepage vhost (same origin) so the page can resize
itself by poking `window.frameElement.style.height`. Stdlib only.
"""

import datetime
import html
import http.client
import json
import os
import pathlib
import ssl
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

FETCH_TIMEOUT = 5
MAX_FETCH_WORKERS = 8

# Heroicons-mini filled status glyphs swapped in for the text pill below the
# narrow-viewport breakpoint. Filled style (fill=currentColor) so the inside
# of the circle/triangle is solid, with the exclamation cut out via fill-rule
# evenodd to let the row background show through.
STATUS_ICONS = {
    "CRITICAL": (
        '<svg class="status-icon" viewBox="0 0 24 24" fill="currentColor" '
        'aria-hidden="true"><path fill-rule="evenodd" '
        'd="M2.25 12c0-5.385 4.365-9.75 9.75-9.75s9.75 4.365 9.75 9.75-4.365 '
        "9.75-9.75 9.75S2.25 17.385 2.25 12Zm9.75-3.75a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 "
        "1-1.5 0V9a.75.75 0 0 1 .75-.75Zm0 8.25a.875.875 0 1 0 0-1.75.875.875 0 0 0 0 "
        '1.75Z" clip-rule="evenodd"/></svg>'
    ),
    "WARNING": (
        '<svg class="status-icon" viewBox="0 0 24 24" fill="currentColor" '
        'aria-hidden="true"><path fill-rule="evenodd" '
        'd="M9.401 3.003c1.155-2 4.043-2 5.197 0l7.355 12.748c1.154 2-.29 '
        "4.5-2.599 4.5H4.645c-2.309 0-3.752-2.5-2.598-4.5L9.4 3.003ZM12 8.25a.75.75 0 0 1 "
        ".75.75v3.75a.75.75 0 0 1-1.5 0V9a.75.75 0 0 1 .75-.75Zm0 8.25a.875.875 0 1 0 "
        '0-1.75.875.875 0 0 0 0 1.75Z" clip-rule="evenodd"/></svg>'
    ),
}

# Trust the host's CA bundle. netdata vhosts use Let's Encrypt via the certbot
# role, so default verification works; no need for a custom store.
SSL_CTX = ssl.create_default_context()


def parse_hosts(spec: str) -> list[tuple[str, str, str]]:
    """Parse `NETDATA_HOSTS` env into (name, query_url, click_url) tuples.

    Format per entry: `name=query[=click]`, entries comma-separated. `click`
    can itself contain `=` (e.g. cloud URLs with query strings); `query`
    cannot — keep query URLs path-only or url-encode any `=` they need.
    Malformed entries are skipped with a stderr warning so a single typo
    doesn't take the whole render offline.
    """
    out = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        # split capped at 3 so click can carry its own `=` (URL query strings).
        parts = [p.strip() for p in chunk.split("=", 2)]
        if len(parts) == 2:
            name, query = parts
            click = query
        elif len(parts) == 3:
            name, query, click = parts
        else:
            print(f"NETDATA_HOSTS skipping malformed entry: {chunk!r}", file=sys.stderr)
            continue
        out.append((name, query.rstrip("/"), click.rstrip("/")))
    return out


class _HostClient:
    """One netdata host's persistent connection for the duration of a single
    _fetch_one call. Reuses one TLS handshake across the 2-3 requests we make
    per host per run (alarms + alarm_log + per-chart fallbacks).

    Invariant: each request fully drains the response before the next one.
    http.client.HTTPConnection raises ResponseNotReady on the next
    getresponse() if a prior response body wasn't consumed -- _get_json's
    unconditional resp.read() + resp.close() satisfies that today, but a
    future switch to streaming reads must preserve it or reuse will break
    silently (the bare except in _fetch_one swallows it as entry['error']).
    """

    def __init__(self, base_url: str, auth_token: str = "") -> None:
        u = urllib.parse.urlsplit(base_url)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        kwargs: dict = {"timeout": FETCH_TIMEOUT}
        if u.scheme == "https":
            kwargs["context"] = SSL_CTX
        self._conn = cls(u.netloc, **kwargs)
        # Token threaded into every request as ?auth_token=<value> (or
        # &auth_token= if the path already carries a query). nginx's
        # bypass_query_param helper short-circuits Authelia and strips
        # the param before proxy_pass so netdata itself never sees it.
        self._auth_token = auth_token

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _get_json(self, path: str) -> dict:
        if self._auth_token:
            sep = "&" if "?" in path else "?"
            path = f"{path}{sep}auth_token={urllib.parse.quote(self._auth_token, safe='')}"
        self._conn.request("GET", path, headers={"User-Agent": "homepage-alerts/1"})
        resp = self._conn.getresponse()
        try:
            body = resp.read()
        finally:
            resp.close()
        if resp.status != 200:
            raise OSError(f"HTTP {resp.status} on {path}")
        return json.loads(body)

    def alarms(self) -> dict:
        return self._get_json("/api/v1/alarms?active")

    def alarm_log(self, *, chart: str | None = None) -> dict:
        # Bulk: count=10000 covers a chatty agent's full retained log on lab; the
        # earlier count=500 was missing alarms whose last transition was >24h old.
        # Chart-filtered: per-alarm fallback when the bulk fetch still misses.
        qs = {"count": "10000"}
        if chart:
            qs["chart"] = chart
        return self._get_json(f"/api/v1/alarm_log?{urllib.parse.urlencode(qs)}")


def latest_transition_by_alarm(log) -> dict[int, str]:
    """Map alarm id → most-recent transition_id. The active-alarms endpoint
    spells the alarm id as `id` while the alarm-log endpoint spells the same
    integer as `alarm_id`; we key on the alarm-log spelling here and join on
    the alarms-endpoint id at the call site. netdata also returns the log as
    a list (older builds) or a {"data": [...]} envelope (newer builds), so
    we accept either; sorting by `unique_id` (newest first) keeps the first
    hit per alarm id, which is the current-state transition we want to
    deep-link to."""
    if isinstance(log, dict):
        log = log.get("data") or []
    out: dict[int, str] = {}
    for entry in sorted(log, key=lambda e: e.get("unique_id") or 0, reverse=True):
        # Use dict-default lookup not `or` so an alarm_id of 0 isn't falsy-dropped.
        aid = entry.get("alarm_id", entry.get("id"))
        tid = entry.get("transition_id") or entry.get("transition_uuid")
        if aid is not None and tid and aid not in out:
            out[aid] = tid
    return out


def alarm_href(click_root: str, host_name: str, alarm: dict) -> str:
    """Build the v2 deep-link for an alarm row. Falls back to the alerts
    list page if alarm_log didn't yield a transition_id."""
    base = f"{click_root}/v2/spaces/{urllib.parse.quote(host_name)}/rooms/local/alerts"
    tid = alarm.get("transition_id")
    if not tid:
        return base
    params = urllib.parse.urlencode(
        {
            "host": host_name,
            "chart": alarm.get("chart", ""),
            "alarm": alarm.get("name", ""),
            "alarm_id": alarm.get("id", ""),
            "alarm_status": alarm.get("status", ""),
            "alarm_chart": alarm.get("chart", ""),
            "alarm_value": alarm.get("value", ""),
            "alarm_when": alarm.get("when", ""),
            "transition_id": tid,
        }
    )
    return f"{base}/{urllib.parse.quote(tid)}?{params}"


def _humanize_delta(seconds: float) -> str:
    """Compact relative-time string like `6d ago` / `3h from now` / `just now`."""
    abs_s = abs(seconds)
    if abs_s < 45:
        return "just now"
    suffix = "ago" if seconds >= 0 else "from now"
    if abs_s < 3600:
        return f"{int(abs_s / 60)}m {suffix}"
    if abs_s < 86400:
        return f"{int(abs_s / 3600)}h {suffix}"
    if abs_s < 86400 * 30:
        return f"{int(abs_s / 86400)}d {suffix}"
    if abs_s < 86400 * 365:
        return f"{int(abs_s / (86400 * 30))}mo {suffix}"
    return f"{int(abs_s / (86400 * 365))}y {suffix}"


def _format_value(alarm: dict) -> str:
    """Pretty-print an alarm value. The `value_string` netdata returns is
    `<num> <unit>` which is fine for `%`/`B`/`s`/etc. but unreadable for
    `unit=timestamp` (raw epoch seconds) and a few sentinel cases. Convert
    timestamps to relative time and `0 timestamp` to `never`."""
    units = (alarm.get("units") or "").strip()
    if units == "timestamp":
        try:
            ts = int(float(alarm.get("value") or 0))
        except (TypeError, ValueError):
            return alarm.get("value_string") or ""
        if ts == 0:
            return "never"
        return _humanize_delta(time.time() - ts)
    return alarm.get("value_string") or ""


def normalize(payload: dict) -> list[dict]:
    """Flatten netdata's `{alarms: {chart.alarm: {...}}}` into a list sorted
    critical-first then warning-first then alphabetical. The `id` and
    `last_status_change` fields are kept so click-through can build the
    same registry-alert-redirect URL netdata uses for telegram notifs."""
    rank = {"CRITICAL": 0, "WARNING": 1, "CLEAR": 2, "UNDEFINED": 3, "UNINITIALIZED": 4}
    items = []
    for key, alarm in (payload.get("alarms") or {}).items():
        items.append(
            {
                "key": key,
                "id": alarm.get("id") or 0,
                "name": alarm.get("name") or key,
                "chart": alarm.get("chart") or "",
                "status": alarm.get("status") or "UNDEFINED",
                "value": _format_value(alarm),
                "when": alarm.get("last_status_change") or 0,
                "info": alarm.get("info") or "",
            }
        )
    items.sort(key=lambda a: (rank.get(a["status"], 99), a["key"]))
    return items


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _fetch_one(name: str, query_url: str, click_url: str, auth_token: str = "") -> dict:
    entry = {"name": name, "url": query_url, "click_url": click_url}
    client: _HostClient | None = None
    try:
        # Loopback URLs skip nginx entirely (and thus skip the Authelia
        # gate), so the auth_token is only applied to off-host targets.
        host = urllib.parse.urlsplit(query_url).hostname or ""
        token_for_client = auth_token if host.lower() not in _LOOPBACK_HOSTS else ""
        client = _HostClient(query_url, auth_token=token_for_client)
        alarms = normalize(client.alarms())
        # alarm_log gives us per-alarm transition_id needed for the v2
        # deep-link path. Bulk fetch first; for any active alarm not in the
        # window, fall back to a chart-filtered fetch since per-chart logs
        # stay short even when the bulk has been GC'd at the tail. All
        # alarm_log failures are non-fatal — the alarm row falls back to the
        # alerts list URL.
        log_warn = ""
        try:
            tid_by_id = latest_transition_by_alarm(client.alarm_log())
        except Exception as e:  # noqa: BLE001
            tid_by_id = {}
            log_warn = f"alarm_log fetch failed: {type(e).__name__}: {e}"
        # Budget for per-chart fallbacks so a single chatty host doesn't push
        # the run past systemd's TimeoutStartSec. Alarms whose transition_id
        # we can't recover within the budget fall back to the alerts list URL,
        # which is a graceful degradation (the row still renders + clicks
        # through, it just doesn't deep-link to the specific transition).
        budget_end = time.monotonic() + FETCH_TIMEOUT
        for a in alarms:
            tid = tid_by_id.get(a["id"], "")
            if not tid and a["chart"] and time.monotonic() < budget_end:
                try:
                    chart_map = latest_transition_by_alarm(client.alarm_log(chart=a["chart"]))
                    tid = chart_map.get(a["id"], "")
                except Exception:  # noqa: BLE001
                    pass
            a["transition_id"] = tid
            a["href"] = alarm_href(click_url, name, a)
        if alarms and not any(a["transition_id"] for a in alarms):
            log_warn = log_warn or "no transition_id matched any active alarm"
        if log_warn:
            print(f"[{name}] {log_warn}", file=sys.stderr)
            entry["log_warn"] = log_warn
        entry["alarms"] = alarms
    except Exception as e:  # noqa: BLE001
        # Bare except so one host's transport quirk (TLS, DNS, weird upstream)
        # never disappears the rest of the dashboard. Log the full detail to
        # the journal but surface only the exception class to the rendered
        # HTML — http.client / ssl error messages otherwise leak internal
        # hostnames / IPs / TLS subject details into the dashboard, which is
        # reachable to anyone on the homepage vhost.
        print(f"[{name}] fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        entry["error"] = type(e).__name__
        entry["alarms"] = []
    finally:
        if client is not None:
            client.close()
    return entry


def collect(hosts: list[tuple[str, str, str]], auth_token: str = "") -> list[dict]:
    # Parallel fetches so one slow/unreachable host doesn't gate the others —
    # worst-case render is bounded by FETCH_TIMEOUT, not summed across hosts.
    # Iterating futures in submit order preserves the configured host order
    # in the response. Cap at MAX_FETCH_WORKERS so a future operator adding
    # 30 hosts doesn't spawn 30 threads on a single run.
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=min(len(hosts), MAX_FETCH_WORKERS)) as pool:
        futures = [pool.submit(_fetch_one, n, q, c, auth_token) for n, q, c in hosts]
        return [f.result() for f in futures]


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Active alerts</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: transparent;
    --fg: #e2e8f0;
    --muted: #64748b;
    --critical: #ef4444;
    --warning: #f59e0b;
    --ok: #10b981;
    --row: rgba(30, 41, 59, 0.6);
  }}
  html, body {{ margin: 0; }}
  html {{ padding: 0; }}
  body {{
    padding: 10px 14px;
    font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--fg);
  }}
  .host {{ margin-bottom: 10px; }}
  .host:last-child {{ margin-bottom: 0; }}
  .host h2 {{
    margin: 0 0 4px 0;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
  }}
  .empty {{ color: var(--ok); padding: 4px 8px; }}
  .err {{ color: var(--critical); padding: 4px 8px; font-style: italic; }}
  a.alarm {{
    display: grid;
    grid-template-columns: auto 1fr auto;
    column-gap: 10px;
    padding: 6px 10px;
    background: var(--row);
    border-radius: 4px;
    margin-bottom: 2px;
    align-items: start;
    text-decoration: none;
    color: inherit;
  }}
  a.alarm:hover {{ background: rgba(51, 65, 85, 0.7); }}
  a.alarm:last-child {{ margin-bottom: 0; }}
  .alarm .status {{
    font-weight: 700;
    font-size: 10px;
    letter-spacing: 0.05em;
    line-height: 1.4;
    white-space: nowrap;
  }}
  .alarm.CRITICAL .status {{ color: var(--critical); }}
  .alarm.WARNING  .status {{ color: var(--warning); }}
  .status-icon {{ display: none; width: 18px; height: 18px; vertical-align: middle; }}
  @media (max-width: 480px) {{
    /* Drop the text label on narrow viewports; the filled glyph alone carries
       the severity so the alarm name has room to read. */
    .alarm .status .status-text {{ display: none; }}
    .alarm .status .status-icon {{ display: inline-block; }}
  }}
  .alarm .name {{
    display: flex;
    flex-direction: column;
    min-width: 0;
    line-height: 1.4;
  }}
  .alarm .name .alarm-name {{ font-weight: 500; word-break: break-word; }}
  .alarm .name code {{
    display: block;
    color: var(--muted);
    font-size: 11px;
    /* Chart slugs carry the differentiating label (`job=<x>`) at the tail —
       ellipsis truncation hid exactly the part that distinguishes two alerts
       on the same chart family. Let it wrap; overflow-wrap: anywhere breaks
       cleanly at the natural separators (./-/_/=) before falling back to
       mid-word splits. */
    overflow-wrap: anywhere;
    line-height: 1.3;
  }}
  .alarm .value {{
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    line-height: 1.4;
  }}
  footer {{
    margin-top: 8px;
    padding: 4px 8px;
    font-size: 11px;
    color: var(--muted);
  }}
</style>
</head>
<body>
{body}
<script>
  // Same-origin auto-height: the alerts panel is mounted under /alerts/ on
  // the homepage vhost so we can directly resize the embedding iframe. The
  // try/catch keeps the page functional if it ever ends up cross-origin.
  (function () {{
    function fit() {{
      try {{
        var f = window.frameElement;
        if (f) f.style.height = document.documentElement.scrollHeight + 'px';
      }} catch (e) {{ /* cross-origin or detached */ }}
    }}
    window.addEventListener('load', fit);
    if (document.readyState === 'complete') fit();
    if (typeof ResizeObserver === 'function') {{
      new ResizeObserver(fit).observe(document.body);
    }}
  }})();
</script>
</body>
</html>
"""


def render_html(hosts: list[dict], iso_updated_at: str) -> str:
    sections = []
    for host in hosts:
        rows = []
        title = html.escape(host["name"])
        click_root = host["click_url"]
        if host.get("error"):
            rows.append(f'<div class="err">{html.escape(host["error"])}</div>')
        elif not host["alarms"]:
            rows.append('<div class="empty">No active alerts.</div>')
        else:
            for a in host["alarms"]:
                # href was pre-computed in _fetch_one so it's also visible on
                # api_alerts.json (debugging) — render_html just trusts it.
                # target=_blank: this page lives in the /alerts/ iframe on the
                # homepage vhost; a _self click would replace the iframe
                # contents, not navigate the parent page. settings.yaml.j2's
                # global `target: _self` is the corresponding half.
                href = a.get("href") or click_root
                rows.append(
                    f'<a class="alarm {html.escape(a["status"])}" '
                    f'href="{html.escape(href)}" target="_blank" rel="noopener">'
                    f'<span class="status">'
                    f'{STATUS_ICONS.get(a["status"], "")}'
                    f'<span class="status-text">{html.escape(a["status"])}</span>'
                    f"</span>"
                    f'<span class="name">'
                    f'<span class="alarm-name">{html.escape(a["name"])}</span>'
                    f'<code>{html.escape(a["chart"])}</code>'
                    f"</span>"
                    f'<span class="value">{html.escape(a["value"])}</span>'
                    f"</a>"
                )
        sections.append(f'<section class="host"><h2>{title}</h2>{"".join(rows)}</section>')
    # Footer shows the wall-clock ISO timestamp of this run. The iframe widget
    # refreshes us every 30s so an operator comparing against the dashboard's
    # datetime widget can spot a wedged generator at a glance. We don't render
    # "X ago" client-side because the python _humanize_delta isn't worth
    # replicating in JS for a footer; a wedged generator also fails the
    # systemd unit, which trips systemdunits monitoring independently.
    footer = f"<footer>Updated {html.escape(iso_updated_at)}</footer>"
    return PAGE.format(body="".join(sections) + footer)


def _atomic_write(path: pathlib.Path, content: str) -> None:
    """Write `content` to `path` via .tmp + os.replace so a concurrent reader
    (nginx) never observes a half-written file. The .tmp name embeds the PID
    so a previous killed run's leftover doesn't race against ours on the
    rename, and so two concurrent runs (shouldn't happen under our timer but
    cheap insurance) don't clobber each other's intermediate."""
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def main() -> None:
    hosts = parse_hosts(os.environ.get("NETDATA_HOSTS", ""))
    if not hosts:
        print("NETDATA_HOSTS is empty; refusing to run", file=sys.stderr)
        sys.exit(1)
    output_dir = pathlib.Path(os.environ.get("OUTPUT_DIR", "/var/www/homepage_alerts"))
    auth_token = os.environ.get("NETDATA_ALERTS_TOKEN", "")

    data = collect(hosts, auth_token=auth_token)
    iso = datetime.datetime.now(tz=datetime.timezone.utc).isoformat(timespec="seconds")

    # No top-level try/except — an exception here (disk full, permission
    # denied, etc.) propagates to Python's default print-traceback-and-exit-1.
    # The traceback lands in the journal via stderr, the unit transitions to
    # failed, and nginx keeps serving the previous (atomic-write means there
    # is no half-written intermediate) snapshot. systemd_service_unit_failed_state
    # picks up the failure independently.
    _atomic_write(output_dir / "index.html", render_html(data, iso))


if __name__ == "__main__":
    main()
