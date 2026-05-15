#!/usr/bin/env python3
"""HTTP listener that stands in for fluent-bit during csplogger _verify.

csplogger's job is to gate browser CSP-report POSTs before they reach
fluent-bit on loopback. To verify the gating without standing up
fluent-bit (and transitively hyperdx) the test harness points nginx at
this stub on the same port fluent-bit would occupy (service_ports.
csp_ingest). It returns 204 (matching fluent-bit's HTTP-input contract
so nginx's 204 to the browser is real) and appends every accepted POST
as one JSON record per line to LOGS_DIRECTORY/log so _verify can grep
for what reached upstream -- crucially that nginx rewrote Content-Type
to application/json, and that blocked probes (foreign Origin, bad
Content-Type, oversize) wrote nothing.
"""
import http.server
import json
import os
from pathlib import Path

LOG_PATH = Path(os.environ["LOGS_DIRECTORY"]) / "log"
LISTEN_PORT = int(os.environ["LISTEN_PORT"])


class Handler(http.server.BaseHTTPRequestHandler):
    # Speak HTTP/1.1 so nginx's `proxy_http_version 1.1` + Connection ""
    # keepalive talks to a matching version on this side; real fluent-bit
    # speaks 1.1 too, so this also makes the stub a more accurate stand-in.
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        record = {
            "content_type": self.headers.get("Content-Type", ""),
            "body": body.decode("utf-8", "replace"),
        }
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_a, **_kw):
        return


if __name__ == "__main__":
    # Touch on startup so _verify's slurp always finds the file, even
    # if every probe is blocked at nginx and zero records ever land.
    LOG_PATH.touch()
    http.server.HTTPServer(("127.0.0.1", LISTEN_PORT), Handler).serve_forever()
