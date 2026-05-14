#!/usr/bin/env python3
"""Prometheus exporter for host-specific signals not covered by netdata's
built-in collectors:
  * /var/log/jobs/* cron job last-success / next-run timestamps
  * netdata context and collector *presence*, via the local netdata API

Per-context staleness and per-collector staleness/failure are handled natively
in roles/custom_exporter/templates/context_liveness.conf.j2 (using
$last_collected_t on each chart and the netdata.plugin_data_collection_status
charts' success/failed dimensions). What native templates can't catch is the
case where the configured chart or collector never appeared in netdata's chart
registry -- a template with on:<missing-chart> silently fails to instantiate.
The netdata_context_present and netdata_collector_present metrics here close
that gap: 1 if the configured id resolves to a registered chart, 0 otherwise.

Drive power state was the third leg of this exporter; it now ships as the
netdata charts.d collector at roles/hdparm/files/hdparm.chart.sh.
"""

from __future__ import annotations

import http.server
import json
import os
import stat
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta

LISTEN = os.environ.get("LISTEN_ADDRESS", "127.0.0.1:19392")
COLLECTORS = [c for c in os.environ.get("NETDATA_COLLECTORS", "").split(",") if c]


def _parse_contexts(raw: str) -> dict[str, str]:
    # Format: name:context_id,name:context_id. partition() splits at the first
    # ':' only, so a context id containing ':' survives intact.
    out: dict[str, str] = {}
    for entry in raw.split(","):
        if not entry:
            continue
        name, sep, ctx = entry.partition(":")
        if not sep:
            raise ValueError(
                f"NETDATA_CONTEXTS entry missing ':' (got {entry!r})"
            )
        out[ctx] = name
    return out


# Parse here, not lazily, so a malformed env surfaces as a Fatal at startup
# rather than ticking exporter_errors silently every 5s.
try:
    CONTEXTS = _parse_contexts(os.environ.get("NETDATA_CONTEXTS", ""))
except ValueError as e:
    print(f"fatal: {e}", file=sys.stderr)
    sys.exit(1)

_lock = threading.Lock()
_errors = 0
_text = "exporter_up 0\n"


def _bump(freq: str, t: datetime) -> datetime | None:
    # retry=2: tolerate one missed run before the alert fires.
    if freq == "hourly":
        return t + timedelta(hours=2)
    if freq == "daily":
        return t + timedelta(days=2)
    if freq == "weekly":
        return t + timedelta(days=14)
    if freq == "monthly":
        # Calendar-month arithmetic, clamping day to the target month's end so
        # Jan 31 + 2 months -> Mar 31, not a Feb-31 ValueError.
        m = t.month - 1 + 2
        year, month = t.year + m // 12, m % 12 + 1
        nxt_m, nxt_y = (month + 1, year) if month < 12 else (1, year + 1)
        last = (datetime(nxt_y, nxt_m, 1, tzinfo=t.tzinfo) - timedelta(days=1)).day
        return t.replace(year=year, month=month, day=min(t.day, last))
    return None


def _cron_lines() -> tuple[list[str], bool]:
    out: list[str] = []
    err = False
    try:
        names = os.listdir("/var/log/jobs")
    except OSError:
        return out, True
    for name in names:
        # /var/log/jobs is mode 0777 in prod; ignore non-regular and dotfiles
        # so a stray entry can't tick exporter_errors every 5s.
        if name.startswith("."):
            continue
        p = f"/var/log/jobs/{name}"
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        try:
            with open(p) as f:
                content = f.read().strip()
        except OSError:
            err = True
            continue
        last_v, next_v = 0, 0
        if content:
            try:
                freq, ts = content.split(" ", 1)
                # Python <3.11's fromisoformat doesn't accept a trailing 'Z' for
                # UTC; cron and ansible_date_time both emit that form. Rewrite
                # it to the explicit offset RFC 3339 also accepts.
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                t = datetime.fromisoformat(ts)
                n = _bump(freq, t)
                if n is None:
                    raise ValueError(f"unknown frequency {freq!r}")
                last_v = int(t.timestamp())
                next_v = int(n.timestamp())
            except (ValueError, KeyError):
                # Malformed date / unknown freq: skip emission for this job and
                # tick errors. Sibling jobs still get their gauges -- matches
                # the Go behaviour and what _verify.yml asserts.
                err = True
                continue
        out.append(f'cron_last_success_timestamp{{job="{name}"}} {last_v}')
        out.append(f'cron_next_run_timestamp{{job="{name}"}} {next_v}')
    return out, err


def _netdata_json(path: str):
    # Bounded client: a wedged netdata would otherwise stall the gather loop
    # while exporter_up keeps reporting 1 to its own scraper.
    with urllib.request.urlopen(
        f"http://localhost:19999{path}", timeout=3
    ) as r:
        if r.status != 200:
            raise RuntimeError(f"netdata {path} -> HTTP {r.status}")
        return json.load(r)


def _netdata_context_present_lines() -> tuple[list[str], bool]:
    if not CONTEXTS:
        return [], False
    try:
        data = _netdata_json("/api/v2/contexts")
    except Exception:
        return [], True
    present = set(data.get("contexts", {}).keys())
    # Iterate CONTEXTS so the metric is emitted for every *configured* context,
    # including ones not in the API response (those land as 0 -> alert fires).
    return (
        [f'netdata_context_present{{context="{name}"}} {int(ctx_id in present)}'
         for ctx_id, name in CONTEXTS.items()],
        False,
    )


def _collector_chart(cid: str) -> str | None:
    # go.d:collector:<plugin>:<job> -> netdata.go_d_<plugin>[_<job>]_data_collection_status
    # When plugin == job (e.g. zfspool:zfspool), netdata dedupes the suffix.
    parts = cid.split(":")
    if len(parts) < 4 or parts[0] != "go.d":
        return None
    plugin, job = parts[2], parts[3]
    suffix = plugin if plugin == job else f"{plugin}_{job}"
    return f"netdata.go_d_{suffix}_data_collection_status"


def _netdata_collector_present_lines() -> tuple[list[str], bool]:
    if not COLLECTORS:
        return [], False
    try:
        data = _netdata_json("/api/v1/charts")
    except Exception:
        return [], True
    charts = set(data.get("charts", {}).keys())
    out = []
    for cid in COLLECTORS:
        expected = _collector_chart(cid)
        present = 1 if expected and expected in charts else 0
        out.append(f'netdata_collector_present{{collector="{cid}"}} {present}')
    return out, False


def _gather() -> None:
    global _errors, _text
    while True:
        errs = 0
        lines = ["exporter_up 1"]
        for fn in (_cron_lines,
                   _netdata_context_present_lines,
                   _netdata_collector_present_lines):
            try:
                got, e = fn()
                lines += got
                errs += int(bool(e))
            except Exception as exc:
                print(f"gather error in {fn.__name__}: {exc}", file=sys.stderr)
                errs += 1
        with _lock:
            _errors += errs
            lines.append(f"exporter_errors {_errors}")
            lines.append(f"gather_last_iteration_timestamp {int(time.time())}")
            _text = "\n".join(lines) + "\n"
        time.sleep(5)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path != "/metrics":
            self.send_error(404)
            return
        with _lock:
            body = _text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):  # silence per-request access log
        return


def main() -> None:
    threading.Thread(target=_gather, daemon=True).start()
    host, port = LISTEN.rsplit(":", 1)
    srv = http.server.ThreadingHTTPServer((host, int(port)), _Handler)
    print(f"Beginning to serve on address `{LISTEN}`", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
