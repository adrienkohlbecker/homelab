"""Tiny stdlib-only HTTP server that aggregates netdata's `/api/v1/alarms?active`
endpoint across one or more hosts and renders both:

- GET /          → HTML page (auto-refresh, dark theme, iframe-friendly)
- GET /api/alerts → JSON: {"hosts": [{"name", "url", "click_url", "alarms": [...], "error"?}]}

Inputs (env):
- NETDATA_HOSTS: comma-separated list of `<name>=<query-url>[=<click-url>]`
  triples. The query-url is fetched server-side, the click-url is the public
  netdata vhost a browser can navigate to. If only one URL is given, both are
  set to it.
  Example: "lab=http://localhost:19999=https://netdata.lab.fahm.fr,pug=https://netdata.pug.fahm.fr"
- LISTEN_PORT:  TCP port to bind on 127.0.0.1 (default 8765)

Embedded in the homepage dashboard via an iframe widget mounted at /alerts/
on the homepage vhost (same origin) so the page can resize itself by
poking `window.frameElement.style.height`. Stdlib only so it runs as a plain
`python3 alerts_server.py` systemd unit on the host — no container, no
third-party deps.
"""

import html
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REFRESH_SECONDS = 30
FETCH_TIMEOUT = 5

# Trust the host's CA bundle. netdata vhosts use Let's Encrypt via the certbot
# role, so default verification works; no need for a custom store.
SSL_CTX = ssl.create_default_context()


def parse_hosts(spec: str) -> list[tuple[str, str, str]]:
    out = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("=")]
        if len(parts) == 2:
            name, query = parts
            click = query
        elif len(parts) == 3:
            name, query, click = parts
        else:
            raise ValueError(f"NETDATA_HOSTS entry malformed: {chunk!r}")
        out.append((name, query.rstrip("/"), click.rstrip("/")))
    return out


def fetch_alarms(url: str) -> dict:
    req = urllib.request.Request(
        f"{url}/api/v1/alarms?active",
        headers={"User-Agent": "homepage-alerts/1"},
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=SSL_CTX) as resp:
        return json.load(resp)


def normalize(payload: dict) -> list[dict]:
    """Flatten netdata's `{alarms: {chart.alarm: {...}}}` into a list sorted
    critical-first then warning-first then alphabetical."""
    rank = {"CRITICAL": 0, "WARNING": 1, "CLEAR": 2, "UNDEFINED": 3, "UNINITIALIZED": 4}
    items = []
    for key, alarm in (payload.get("alarms") or {}).items():
        items.append(
            {
                "key": key,
                "name": alarm.get("name") or key,
                "chart": alarm.get("chart") or "",
                "status": alarm.get("status") or "UNDEFINED",
                "value": alarm.get("value_string") or "",
                "info": alarm.get("info") or "",
            }
        )
    items.sort(key=lambda a: (rank.get(a["status"], 99), a["key"]))
    return items


def _fetch_one(name: str, query_url: str, click_url: str) -> dict:
    entry = {"name": name, "url": query_url, "click_url": click_url}
    try:
        entry["alarms"] = normalize(fetch_alarms(query_url))
    except Exception as e:  # noqa: BLE001
        # Bare except so one host's transport quirk (TLS, DNS, weird upstream)
        # never disappears the rest of the dashboard. The error string lands
        # in the rendered HTML so the operator can see what's up.
        entry["error"] = f"{type(e).__name__}: {e}"
        entry["alarms"] = []
    return entry


def collect(hosts: list[tuple[str, str, str]]) -> list[dict]:
    # Parallel fetches so one slow/unreachable host doesn't gate the others —
    # worst-case page render is bounded by FETCH_TIMEOUT, not summed across
    # hosts. Iterating futures in submit order preserves the configured host
    # order in the response.
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        futures = [pool.submit(_fetch_one, n, q, c) for n, q, c in hosts]
        return [f.result() for f in futures]


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh}">
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
    grid-template-columns: 80px 1fr auto;
    gap: 8px;
    padding: 5px 8px;
    background: var(--row);
    border-radius: 4px;
    margin-bottom: 2px;
    align-items: baseline;
    text-decoration: none;
    color: inherit;
  }}
  a.alarm:hover {{ background: rgba(51, 65, 85, 0.7); }}
  a.alarm:last-child {{ margin-bottom: 0; }}
  .alarm .status {{
    font-weight: 600;
    font-size: 11px;
    letter-spacing: 0.04em;
  }}
  .alarm.CRITICAL .status {{ color: var(--critical); }}
  .alarm.WARNING  .status {{ color: var(--warning); }}
  .alarm .name {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .alarm .name code {{ color: var(--muted); font-size: 11px; }}
  .alarm .value {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
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


def render_html(hosts: list[dict]) -> str:
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
                # Deep-link to the alarm's chart in netdata so the click lands
                # on the actual metric, not just the alarms tab. Opens in a
                # new tab — keeps the dashboard available behind it.
                href = f"{click_root}/#;chart={urllib.parse.quote(a['chart'], safe='._-')}"
                rows.append(
                    f'<a class="alarm {html.escape(a["status"])}" '
                    f'href="{html.escape(href)}" target="_blank" rel="noopener">'
                    f'<span class="status">{html.escape(a["status"])}</span>'
                    f'<span class="name">{html.escape(a["name"])} '
                    f'<code>{html.escape(a["chart"])}</code></span>'
                    f'<span class="value">{html.escape(a["value"])}</span>'
                    f"</a>"
                )
        sections.append(f'<section class="host"><h2>{title}</h2>{"".join(rows)}</section>')
    return PAGE.format(refresh=REFRESH_SECONDS, body="".join(sections))


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path in ("/", "/index.html"):
            data = collect(self.server.hosts)
            self._send(200, "text/html; charset=utf-8", render_html(data).encode("utf-8"))
        elif self.path == "/api/alerts":
            data = collect(self.server.hosts)
            self._send(200, "application/json", json.dumps({"hosts": data}).encode("utf-8"))
        elif self.path == "/api/healthcheck":
            self._send(200, "application/json", b'{"status":"ok"}')
        else:
            self._send(404, "text/plain", b"not found")

    def log_message(self, fmt: str, *args) -> None:
        # Silence per-request access logs; systemd journal still gets stderr.
        return


def main() -> None:
    hosts = parse_hosts(os.environ.get("NETDATA_HOSTS", ""))
    if not hosts:
        print("NETDATA_HOSTS is empty; refusing to start", file=sys.stderr)
        sys.exit(1)
    port = int(os.environ.get("LISTEN_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.hosts = hosts  # attach to instance so handlers can read it
    print(f"alerts_server listening on 127.0.0.1:{port} for {len(hosts)} host(s)", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
