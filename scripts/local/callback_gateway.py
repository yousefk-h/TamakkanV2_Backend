"""Minimal, locked-down gateway for a cloud-hosted DEVELOPMENT Mariam agent.

A hosted agent must reach the local backend over the public internet (via a tunnel such as
`cloudflared tunnel --url http://127.0.0.1:4674`). Pointing a tunnel at the whole local API
would expose it; point it at THIS gateway instead. It forwards only:

    POST /api/v1/sessions/<session id>/mark-connected
    POST /api/v1/sessions/<session id>/mark-ended

and only when the X-Internal-Api-Key header matches the dev AGENT_INTERNAL_API_KEY. Everything
else gets 404 without reaching the backend (which checks the key again). Loopback bind only.

    python scripts/local/callback_gateway.py --env .env [--port 4674]
"""
from __future__ import annotations

import argparse
import hmac
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Session ids are UUIDs (hex + dashes); a 24-hex ObjectId form is accepted too.
ALLOWED = re.compile(r"^/api/v1/sessions/[0-9a-fA-F][0-9a-fA-F-]{7,63}/(mark-connected|mark-ended)$")
MAX_BODY = 512 * 1024  # a transcript can be sizeable, but never unbounded


def make_handler(upstream: str, key: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "callback-gateway"

        def log_message(self, fmt, *args):  # no bodies, no headers in logs
            print(f"{self.command} {self.path.split('?')[0]} -> {getattr(self, '_status', '?')}", flush=True)

        def _reply(self, status: int, body: bytes = b"", ctype: str = "application/json"):
            self._status = status
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            path = self.path.split("?")[0]
            if not ALLOWED.match(path):
                return self._reply(404, b'{"detail":"Not Found"}')
            supplied = self.headers.get("X-Internal-Api-Key", "")
            if not key or not hmac.compare_digest(supplied.encode(), key.encode()):
                return self._reply(403, b'{"detail":"Forbidden"}')
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._reply(413, b'{"detail":"Too large"}')
            data = self.rfile.read(length) if length else b""
            req = urllib.request.Request(upstream + path, data=data, method="POST",
                                         headers={"Content-Type": "application/json", "X-Internal-Api-Key": supplied})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return self._reply(r.status, r.read())
            except urllib.error.HTTPError as e:
                return self._reply(e.code, e.read())
            except Exception:
                return self._reply(502, b'{"detail":"Bad gateway"}')

        def do_GET(self):
            self._reply(404, b'{"detail":"Not Found"}')

        do_PUT = do_PATCH = do_DELETE = do_GET

    return Handler


def main() -> int:
    from dotenv import dotenv_values

    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True)
    ap.add_argument("--port", type=int, default=4674)
    ap.add_argument("--upstream", default="http://127.0.0.1:4673")
    a = ap.parse_args()
    key = dotenv_values(Path(a.env)).get("AGENT_INTERNAL_API_KEY") or ""
    if not key or key == "GENERATE_ON_SETUP":
        raise SystemExit("AGENT_INTERNAL_API_KEY is not set in the env file")
    if not a.upstream.startswith(("http://127.0.0.1", "http://localhost")):
        raise SystemExit("upstream must be the local backend")
    print(f"callback gateway on 127.0.0.1:{a.port} -> {a.upstream} (mark-connected / mark-ended only)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.upstream.rstrip("/"), key)).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
