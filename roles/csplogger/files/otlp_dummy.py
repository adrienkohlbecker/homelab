#!/usr/bin/env python3
"""Minimal OTLP HTTP receiver for csplogger _verify.

Replaces hyperdx as fluent-bit's OTLP sink during csplogger tests.
Accepts any POST, appends the request body to a log file, and returns
200 OK so fluent-bit doesn't retry. _verify greps the log for a per-run
marker to confirm the nginx -> fluent-bit -> OTLP pipeline propagated
the test report. Far lighter than standing up the real hyperdx stack
(ClickHouse + Mongo + collector + UI) just to assert "a record left
fluent-bit."

Bodies are protobuf-encoded by default; the marker is a UTF-8 URL
string that appears verbatim in the protobuf payload, so `grep -F` on
the raw log catches it without needing to decode.
"""
import http.server
from pathlib import Path

LOG_PATH = Path("/var/log/otlp_dummy.log")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        with LOG_PATH.open("ab") as f:
            f.write(body + b"\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_a, **_kw):
        return


if __name__ == "__main__":
    http.server.HTTPServer(("127.0.0.1", 4318), Handler).serve_forever()
