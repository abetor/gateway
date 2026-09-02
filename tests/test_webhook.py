"""Webhook channel through its real handler and an in-process server stub.

An invalid token returns 403 and an unknown route returns 404 without spawning.
The test verifies this through filesystem effects, not status alone. Accepted
requests return 202 immediately and spawn with a payload file. Every POST enters
the inbound log and a reaper records command exits in the dispatch log.
"""
from __future__ import annotations

import json
import io
import time

import pytest

from tool_gateway.core import log as corelog
from tool_gateway.core.config import load_config
from tool_gateway.ext.webhook.server import WebhookServer

TOKEN = "x" * 16


def _rows(home, stream):
    return [json.loads(line)
            for line in (home / "log" / f"{stream}.jsonl").read_text("utf-8").splitlines()]


SLOW = 2.0          # Deliberately longer than the expected immediate HTTP response.


def _appears(path, timeout: float) -> bool:
    """Poll for asynchronous file creation without a flaky fixed sleep."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


class _ServerStub:
    """Capture the real request handler without binding a network socket."""

    def __init__(self, address, handler_class):
        self.server_address = address
        self.RequestHandlerClass = handler_class

    def serve_forever(self):
        return None

    def shutdown(self):
        return None


@pytest.fixture
def gw(tmp_path, monkeypatch):
    out = tmp_path / "out.txt"
    slow_out = tmp_path / "slow-done.txt"
    (tmp_path / "gateway.toml").write_text(f'''
[channels.webhook]
port = 0

[routes.echo]
command = "cat $GATEWAY_PAYLOAD > {out}; echo $GATEWAY_ROUTE >> {out}"

[routes.slow]
command = "sleep {SLOW}; echo late > {slow_out}"
''', "utf-8")
    monkeypatch.setattr("tool_gateway.ext.webhook.server.ThreadingHTTPServer", _ServerStub)
    g = WebhookServer(load_config(tmp_path).webhook, tmp_path, TOKEN,
                      events=corelog.bind(tmp_path))
    g.start_background()
    yield g, out, tmp_path
    g.stop()


def test_health_lists_routes(gw):
    g, _, _ = gw
    status, body = _request(g, "GET", "/health")
    assert status == 200 and body["routes"] == ["echo", "slow"]


def _request(g, method, path, body=b"", token=TOKEN):
    """Invoke the captured BaseHTTPRequestHandler without opening a socket."""
    handler = object.__new__(g._server.RequestHandlerClass)
    handler.path = path
    handler.headers = {
        "X-Gateway-Token": token,
        "Content-Length": str(len(body)),
    }
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    response = {}
    handler.send_response = lambda code: response.update(status=code)
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    getattr(handler, "do_" + method)()
    return response["status"], json.loads(handler.wfile.getvalue())


def _post(g, path, body=b"", token=TOKEN):
    return _request(g, "POST", path, body=body, token=token)


def test_dispatch_with_logs(gw):
    g, out, home = gw
    code, body = _post(g, "/hook/echo", b'{"hello":1}')
    assert code == 202 and body["accepted"] == "echo"
    for _ in range(50):  # Wait for the asynchronous command's output file.
        if out.exists() and "echo" in out.read_text():
            break
        time.sleep(0.1)
    assert '{"hello":1}' in out.read_text() and "echo" in out.read_text()
    # The payload is a file under home, not an argv value.
    assert list((home / "payloads").iterdir())
    # Acceptance is recorded in the inbound log.
    rows = _rows(home, "inbound")
    assert rows[-1]["route"] == "echo" and rows[-1]["status"] == 202
    assert rows[-1]["payload_bytes"] == len(b'{"hello":1}')
    # The asynchronous reaper appends the command exit to the dispatch log.
    for _ in range(50):
        if (home / "log" / "dispatch.jsonl").exists():
            break
        time.sleep(0.1)
    drow = _rows(home, "dispatch")[-1]
    assert drow["exit"] == 0 and drow["route"] == "echo" and drow["channel"] == "webhook"


def test_auth_and_unknown_route_refused_without_spawn(gw):
    """Refusal prevents spawning, proven by the absence of its output file.

    A control request with the valid token creates that file within the same
    observation window, proving the earlier absence was meaningful.
    """
    g, out, home = gw
    assert _post(g, "/hook/echo", token="wrong")[0] == 403
    assert _post(g, "/hook/nope")[0] == 404
    assert not _appears(out, 1.0), "403/404 spawned a command; refusal must precede spawn"

    t0 = time.time()
    code, _ = _post(g, "/hook/echo", b"x")
    assert code == 202
    assert _appears(out, 5.0)
    assert time.time() - t0 < 1.0, "control spawn exceeded refusal window; earlier check proves nothing"

    statuses = [r["status"] for r in _rows(home, "inbound")]
    assert statuses[:2] == [403, 404] and statuses[-1] == 202  # Refusals are logged too.


def test_202_answers_before_command_finishes(gw):
    """Measure that 202 arrives while the slow command is still running."""
    g, _, home = gw
    slow_out = home / "slow-done.txt"
    t0 = time.time()
    code, body = _post(g, "/hook/slow", b"{}")
    elapsed = time.time() - t0
    assert code == 202 and body["accepted"] == "slow"
    assert elapsed < SLOW / 2, f"response waited for command: {elapsed:.2f}s for {SLOW}s command"
    assert not slow_out.exists(), "command already finished; timing check proves nothing"
