"""Generate the homepage alerts panel's static HTML snapshot.

Polls each configured host's netdata alerts over SSH in parallel and writes
index.html — a dark-themed, iframe-friendly alert list.

Run as a Type=oneshot systemd service fired by homepage_alerts.timer on
a sub-minute cadence (OnCalendar=*:*:0/30 + AccuracySec=1s on the
homepage host). nginx serves the file directly from OUTPUT_DIR; no
proxy_pass, no long-running process.

Single transport: every host — including the converging host's own netdata —
is reached the same way, an SSH call to the netdata_poll forced-command key
that returns a `{"alarms", "transitions"}` JSON bundle (one round trip, no
nginx, no URL token). SSH connections are multiplexed (ControlMaster) so the
30s cadence reuses one warm connection per peer instead of re-handshaking.

Inputs (env):
- NETDATA_HOSTS: comma-separated list of `<name>=<query-url>[=<click-url>]`
  triples. The query-url is the `ssh://netdata_poll@host` to pull from; the
  click-url is the public netdata vhost a browser can navigate to. If only one
  URL is given, both are set to it.
  Example: "lab=ssh://netdata_poll@10.0.0.2=https://netdata.lab.fahm.fr,pug=ssh://netdata_poll@10.0.0.3=https://netdata.pug.fahm.fr"
- OUTPUT_DIR: directory to write the files into (default
  /var/www/homepage_alerts). Must be writable by the unit's User=.
- NETDATA_POLL_SSH_KEY: path to the netdata_poll private key.
- NETDATA_POLL_SSH_KNOWN_HOSTS: path to the known_hosts file pinning the
  peers' host keys (StrictHostKeyChecking=yes).
- NETDATA_POLL_SSH_CONTROL_DIR: directory for the ControlMaster sockets
  (a persisted RuntimeDirectory; default /run/homepage_alerts). All three
  SSH inputs are required.

Embedded in the homepage dashboard via an iframe widget mounted at
/alerts/ on the homepage vhost (same origin) so the page can resize
itself by poking `window.frameElement.style.height`. Stdlib only.
"""

import datetime
import html
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

MAX_FETCH_WORKERS = 8
# SSH transport bounds. SSH_CONNECT_TIMEOUT caps the TCP connect; SSH_TIMEOUT
# caps the whole call (connect + the forced command's two loopback fetches on
# the far side). Both sit well inside the timer's 25s TimeoutStartSec so a
# wedged peer fails the run rather than hanging it. ControlMaster reuse makes
# the steady-state call far cheaper than a cold connect.
SSH_CONNECT_TIMEOUT = 5
SSH_TIMEOUT = 10
# Keep the multiplexed master warm well past the 30s poll cadence so the next
# fire reuses it; it self-reaps this long after polling stops.
SSH_CONTROL_PERSIST = 120

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


class _SshConfig:
    """Static SSH transport config shared by every per-host fetch in one run:
    the netdata_poll private key, the pinned known_hosts, and the ControlMaster
    socket directory. `%C` in the ControlPath hashes (local, remote, port, user)
    so each peer gets its own socket; ControlMaster=auto reuses a warm one or
    opens it, and ControlPersist keeps it alive across the 30s poll cadence."""

    def __init__(self, key: str, known_hosts: str, control_dir: str) -> None:
        if not (key and known_hosts and control_dir):
            raise OSError(
                "NETDATA_POLL_SSH_KEY, NETDATA_POLL_SSH_KNOWN_HOSTS and "
                "NETDATA_POLL_SSH_CONTROL_DIR are all required"
            )
        self.base_opts = [
            "-i",
            key,
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={control_dir.rstrip('/')}/cm-%C",
            "-o",
            f"ControlPersist={SSH_CONTROL_PERSIST}",
        ]


class _SshHostClient:
    """Fetch one host's alert bundle over SSH via the netdata_poll forced-command
    key. A single `ssh` invocation returns a JSON bundle {"alarms": ...,
    "transitions": ...}, cached for the alarms() + alert_transitions() calls
    _fetch_one makes per host. The remote command is fixed by authorized_keys
    (command=), so the argv carries none — whatever we would append is ignored
    server-side; the key cannot run anything else.

    StrictHostKeyChecking=yes + the pinned known_hosts is the trust anchor: a
    peer whose host key isn't pre-registered (roles/homepage keyscan) is refused
    rather than TOFU-accepted.
    """

    def __init__(self, base_url: str, cfg: _SshConfig) -> None:
        u = urllib.parse.urlsplit(base_url)
        self._target = f"{u.username or 'netdata_poll'}@{u.hostname}"
        self._cfg = cfg
        self._bundle: dict | None = None

    def close(self) -> None:
        pass

    def _fetch(self) -> dict:
        if self._bundle is not None:
            return self._bundle
        argv = ["ssh", *self._cfg.base_opts, self._target]
        proc = subprocess.run(argv, capture_output=True, timeout=SSH_TIMEOUT)  # noqa: S603
        if proc.returncode != 0:
            # stderr can leak the peer address / host-key detail; _fetch_one
            # surfaces only the exception class to the rendered HTML, while the
            # full message lands in the journal.
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise OSError(f"ssh {self._target} exited {proc.returncode}: {err}")
        self._bundle = json.loads(proc.stdout)
        return self._bundle

    def alarms(self) -> dict:
        return self._fetch().get("alarms") or {}

    def alert_transitions(self) -> dict:
        return self._fetch().get("transitions") or {}


def latest_transition_by_alarm(log) -> dict[tuple[str, str], str]:
    """Map (alert name, instance) → most-recent transition_id from the netdata
    v2 /api/v2/alert_transitions response. v2 transitions carry `alert` (the
    alarm name) and `instance` (the chart id) instead of the integer alarm_id
    the retired v1 alarm_log used, so we key on the (name, chart) pair the
    active-alarms endpoint joins on at the call site. `gi` is a monotonic
    global id; sorting newest-first keeps the current-state transition we want
    to deep-link to. The payload is the `{"transitions": [...]}` envelope; a
    bare list is also accepted for test fixtures."""
    transitions = log.get("transitions") if isinstance(log, dict) else (log or [])
    out: dict[tuple[str, str], str] = {}
    for entry in sorted(transitions or [], key=lambda e: e.get("gi") or e.get("when") or 0, reverse=True):
        key = (entry.get("alert"), entry.get("instance"))
        tid = entry.get("transition_id")
        if key[0] and key[1] and tid and key not in out:
            out[key] = tid
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


def _fetch_one(name: str, query_url: str, click_url: str, cfg: "_SshConfig") -> dict:
    entry = {"name": name, "url": query_url, "click_url": click_url}
    client = None
    try:
        client = _SshHostClient(query_url, cfg)
        alarms = normalize(client.alarms())
        # The bundle carries the v2 alert_transitions alongside the alarms, so
        # the per-alarm transition_id (needed for the deep-link path) is already
        # in hand — no second round trip. A transitions miss is non-fatal: the
        # alarm row falls back to the alerts list URL.
        log_warn = ""
        try:
            tid_by_key = latest_transition_by_alarm(client.alert_transitions())
        except Exception as e:  # noqa: BLE001
            tid_by_key = {}
            log_warn = f"alert_transitions parse failed: {type(e).__name__}: {e}"
        # The server-side bundle window already covers every active alarm (an
        # active alarm transitioned within it), so the (name, chart) join is a
        # single pass. A miss falls back to the alerts list URL -- the row still
        # renders + clicks through, it just doesn't deep-link to the transition.
        for a in alarms:
            a["transition_id"] = tid_by_key.get((a["name"], a["chart"]), "")
            a["href"] = alarm_href(click_url, name, a)
        if alarms and not any(a["transition_id"] for a in alarms):
            log_warn = log_warn or "no transition_id matched any active alarm"
        if log_warn:
            print(f"[{name}] {log_warn}", file=sys.stderr)
            entry["log_warn"] = log_warn
        entry["alarms"] = alarms
    except Exception as e:  # noqa: BLE001
        # Bare except so one host's transport quirk (SSH down, host-key
        # mismatch, malformed bundle) never disappears the rest of the
        # dashboard. Log the full detail to the journal but surface only the
        # exception class to the rendered HTML — ssh error messages otherwise
        # leak peer addresses / host-key detail into the dashboard, which is
        # reachable to anyone on the homepage vhost.
        print(f"[{name}] fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        entry["error"] = type(e).__name__
        entry["alarms"] = []
    finally:
        if client is not None:
            client.close()
    return entry


def collect(hosts: list[tuple[str, str, str]], cfg: "_SshConfig") -> list[dict]:
    # Parallel fetches so one slow/unreachable host doesn't gate the others —
    # worst-case render is bounded by SSH_TIMEOUT, not summed across hosts.
    # Iterating futures in submit order preserves the configured host order in
    # the response. Cap at MAX_FETCH_WORKERS so a future operator adding 30
    # hosts doesn't spawn 30 threads on a single run.
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=min(len(hosts), MAX_FETCH_WORKERS)) as pool:
        futures = [pool.submit(_fetch_one, n, q, c, cfg) for n, q, c in hosts]
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
    cfg = _SshConfig(
        os.environ.get("NETDATA_POLL_SSH_KEY", ""),
        os.environ.get("NETDATA_POLL_SSH_KNOWN_HOSTS", ""),
        os.environ.get("NETDATA_POLL_SSH_CONTROL_DIR", "/run/homepage_alerts"),
    )

    data = collect(hosts, cfg)
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
