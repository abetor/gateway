"""HTTP listener that accepts ``POST /hook/<route>`` and returns 202.

Security and execution invariants:

- Tokens are compared only with ``hmac.compare_digest``. The value comes from
  the environment through ``core.config.resolve_webhook_token`` and must have
  at least 16 characters, so startup fails closed before binding.
- Commands receive request bodies through a file named by ``GATEWAY_PAYLOAD``.
  Payloads do not enter argv, where process listings could expose them, or
  stdin, which a configured command pipeline might consume incorrectly.
- The server returns 202 without waiting for the command. Long-running work
  belongs in a scheduler or tool that reports its own result.

Every POST, including 403 and 404 refusals, enters the inbound audit log. A
reaper thread records every spawned command's exit status in the dispatch log.
"""
from __future__ import annotations

import hmac
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from ...core.config import WebhookCfg


class WebhookServer:
    def __init__(self, cfg: WebhookCfg, home: str | Path, token: str,
                 events: Optional[Callable] = None):
        self.cfg = cfg
        self.home = Path(home)
        self.token = token
        self.events = events or (lambda stream, **row: None)
        self._server: ThreadingHTTPServer | None = None

    def dispatch(self, route: str, payload: bytes) -> None:
        payload_dir = self.home / "payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        fd, payload_path = tempfile.mkstemp(prefix=f"gw-{route}-", suffix=".json",
                                            dir=str(payload_dir))
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        command = self.cfg.routes[route]
        env = {**os.environ, "GATEWAY_ROUTE": route, "GATEWAY_PAYLOAD": payload_path}
        p = subprocess.Popen(["/bin/sh", "-c", command], env=env,
                             cwd=self.home, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Return 202 immediately while a reaper still records the command exit.
        threading.Thread(target=self._reap, args=(p, route, command), daemon=True).start()

    def _reap(self, p: subprocess.Popen, route: str, command: str) -> None:
        self.events("dispatch", channel="webhook", route=route, command=command, exit=p.wait())

    def make_server(self) -> ThreadingHTTPServer:
        gw = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # Use structured JSONL instead of stderr noise.
                pass

            def _send(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/health":
                    self._send(200, {"ok": True, "routes": sorted(gw.cfg.routes)})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                token = self.headers.get("X-Gateway-Token", "")
                if not hmac.compare_digest(token, gw.token):
                    gw.events("inbound", channel="webhook", path=self.path, status=403)
                    self._send(403, {"error": "bad token"})
                    return
                if not self.path.startswith("/hook/"):
                    gw.events("inbound", channel="webhook", path=self.path, status=404)
                    self._send(404, {"error": "not found"})
                    return
                route = self.path[len("/hook/"):].strip("/")
                if route not in gw.cfg.routes:
                    gw.events("inbound", channel="webhook", path=self.path, status=404)
                    self._send(404, {"error": f"no route {route}"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                payload = self.rfile.read(length) if length else b""
                gw.events("inbound", channel="webhook", route=route, status=202,
                          payload_bytes=len(payload))
                gw.dispatch(route, payload)
                self._send(202, {"accepted": route})

        server = ThreadingHTTPServer((self.cfg.host, self.cfg.port), Handler)
        self._server = server
        return server

    def serve_forever(self) -> None:
        self.make_server().serve_forever()

    def start_background(self) -> threading.Thread:
        """Start a test server in a thread and expose its port as ``actual_port``."""
        server = self.make_server()
        self.actual_port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
