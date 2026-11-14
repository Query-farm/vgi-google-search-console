"""Serve the notebook's explicit synthetic inputs on a loopback-only API."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import unquote, urlsplit


class SyntheticAPI:
    """A narrow HTTP fixture for supported search and inspection read operations."""

    def __init__(self):
        """Load invented inputs and start a disposable loopback server."""
        self.fixture = json.loads((Path(__file__).parent / "fixtures/search_console.json").read_text())
        self.requests = []
        fixture = self.fixture
        request_log = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.respond("GET")

            def do_POST(self):
                self.respond("POST")

            def respond(self, method):
                path = unquote(urlsplit(self.path).path)
                if self.headers.get("Authorization") != "Bearer notebook-synthetic-token":
                    self.reply(401, {"error": {"code": 401, "message": "Synthetic token required"}})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length)) if length else {}
                request_log.append({"method": method, "path": path, "body": body})
                site = fixture["site_url"]
                if method == "GET" and path == "/webmasters/v3/sites":
                    self.reply(200, {"siteEntry": [{"siteUrl": site, "permissionLevel": "siteOwner"}]})
                    return
                if method == "POST" and path == f"/webmasters/v3/sites/{site}/searchAnalytics/query":
                    dims = body.get("dimensions", [])
                    if (
                        body.get("startDate") != fixture["start_date"]
                        or body.get("endDate") != fixture["end_date"]
                        or dims not in (["page", "device"], ["page"])
                        or body.get("dimensionFilterGroups")
                    ):
                        self.reply(
                            400,
                            {
                                "error": {
                                    "code": 400,
                                    "message": "Fixture supports only the documented week and dimensions",
                                }
                            },
                        )
                        return
                    groups = {}
                    for row in fixture["facts"]:
                        key = tuple(row[k] for k in dims)
                        acc = groups.setdefault(key, [0, 0, 0])
                        acc[0] += row["clicks"]
                        acc[1] += row["impressions"]
                        acc[2] += row["position"] * row["impressions"]
                    rows = [
                        {
                            "keys": list(k),
                            "clicks": a[0],
                            "impressions": a[1],
                            "ctr": a[0] / a[1],
                            "position": a[2] / a[1],
                        }
                        for k, a in groups.items()
                    ]
                    rows.sort(key=lambda row: (-row["clicks"], row["keys"]))
                    offset, limit = body.get("startRow", 0), body.get("rowLimit", 25000)
                    self.reply(200, {"responseAggregationType": "byPage", "rows": rows[offset : offset + limit]})
                    return
                if method == "POST" and path == "/v1/urlInspection/index:inspect":
                    url = body.get("inspectionUrl")
                    if body.get("siteUrl") == site and url in fixture["inspections"]:
                        self.reply(200, {"inspectionResult": {"indexStatusResult": fixture["inspections"][url]}})
                        return
                self.reply(404, {"error": {"code": 404, "message": "Outside this synthetic fixture"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        """Stop and join the local fixture server."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
