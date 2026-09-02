"""Telegram loop behavior and per-project serialization.

Tests cover a single pass, durable honesty after acknowledgment, visible channel
failures, shared audit events, and real ``once=False`` queues and workers.
"""
from __future__ import annotations

import threading
import time

from _helpers import FakeChannel, Run, tcfg
from tool_gateway.core.channel import Inbound
from tool_gateway.ext.telegram.serve import serve


def test_serve_once_polls_dispatches_sends(tmp_path):
    cfg = tcfg(tmp_path)
    fake = FakeChannel([Inbound(text="#4 approve")])
    run = Run(out="session #4 complete")
    n = serve(fake, cfg, once=True, run=run, log=lambda *a: None)
    assert n == 1 and fake.outbox == ["session #4 complete"]
    assert run.calls[0][0][2:] == ["rig", "--path", cfg.projects["alpha"].path,
                                   "--reply", "4", "approve"]


def test_serve_once_route_error_answers_without_cli(tmp_path):
    cfg = tcfg(tmp_path, projects=("alpha", "beta"))   # No default.
    fake = FakeChannel([Inbound(text="add feature")])
    run = Run()
    serve(fake, cfg, once=True, run=run, log=lambda *a: None)
    assert not run.calls and "alpha" in fake.outbox[0] and "beta" in fake.outbox[0]


def test_serve_handle_survives_dispatch_crash(tmp_path):
    """A non-timeout turn failure reaches the operator without killing the worker."""
    cfg = tcfg(tmp_path)
    fake = FakeChannel([Inbound(text="task")])

    def run(cmd, **kw):
        raise OSError("python missing")

    n = serve(fake, cfg, once=True, run=run, log=lambda *a: None)
    assert n == 1 and "turn failed" in fake.outbox[0] and "python missing" in fake.outbox[0]


def test_serve_reports_leftover_pending_on_restart(tmp_path):
    """After a crash, report acknowledged leftovers without replay and clear the journal."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "watch-pending.json").write_text(
        '[{"project": "alpha", "kind": "task", "text": "lost task", "ref": null}]',
        "utf-8")
    cfg = tcfg(tmp_path)
    fake = FakeChannel([])
    serve(fake, cfg, once=True, run=Run(), log=lambda *a: None, state_dir=str(state))
    assert any("lost task" in t and "resend" in t for t in fake.outbox)
    assert (state / "watch-pending.json").read_text("utf-8") == "[]"


def test_serve_surfaces_channel_poll_error(tmp_path):
    """An expired token or second-poller 409 remains visible in loop logs."""
    cfg = tcfg(tmp_path)
    logs = []
    fake = FakeChannel([])
    fake.last_error = "Conflict: terminated by other getUpdates"
    serve(fake, cfg, once=True, run=Run(), log=logs.append)
    assert any("Conflict" in ln for ln in logs) and fake.last_error == ""


def test_serve_emits_inbound_dispatch_delivery_events(tmp_path):
    """Inbound, dispatch, and response delivery all enter the shared audit log."""
    cfg = tcfg(tmp_path)
    fake = FakeChannel([Inbound(text="#4 approve", sender="714")])
    events = []
    serve(fake, cfg, once=True, run=Run(out="session #4 complete"), log=lambda *a: None,
          events=lambda stream, **row: events.append((stream, row)))
    streams = [s for s, _ in events]
    assert streams == ["inbound", "dispatch", "delivery"]
    inbound = events[0][1]
    assert inbound["text"] == "#4 approve" and inbound["sender"] == "714"
    delivery = events[2][1]
    assert delivery["sent"] is True and "complete" in delivery["text"]
    assert delivery["source"] == "watch"


# ================================================= per-project serialization
class _StopLoop(Exception):
    """Stop the infinite serve loop by raising on the second poll."""


class _LoopChannel(FakeChannel):
    """Channel for the real loop: one message batch, then a stop exception.

    The outbox is locked because workers from different projects write to it.
    """

    def __init__(self, inbox):
        super().__init__(inbox)
        self._lock = threading.Lock()
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self.polls > 1:
            raise _StopLoop
        return super().poll()

    def send(self, text):
        with self._lock:
            self.outbox.append(text)
        return True


class _Gate:
    """CLI stub that blocks turns so ordering cannot be a fast-call coincidence."""

    def __init__(self, texts):
        self.gates = {t: threading.Event() for t in texts}
        self.started: list = []
        self.finished: list = []
        self._lock = threading.Lock()

    def __call__(self, cmd, **kw):
        text = next(t for t in self.gates if t in cmd)
        with self._lock:
            self.started.append(text)
        assert self.gates[text].wait(timeout=10), f"turn {text!r} was not released"
        with self._lock:
            self.finished.append(text)

        class R:  # noqa: N801 - minimal subprocess result
            returncode, stdout, stderr = 0, f"complete: {text}", ""
        return R()

    def snapshot(self):
        with self._lock:
            return list(self.started), list(self.finished)


def _until(pred, timeout=10.0):
    """Poll a condition instead of using a fixed sleep in a thread test."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_serve_serial_per_project_parallel_across_projects(tmp_path):
    """Turns are serial within a project and parallel across projects.

    A mutation that starts one thread per route lets the second alpha turn start
    before the first finishes and makes this test fail.
    """
    cfg = tcfg(tmp_path, projects=("alpha", "beta"))
    gate = _Gate(["first", "second", "third"])
    gate.gates["second"].set()                     # Block only the first alpha turn.
    gate.gates["third"].set()                      # Beta is free from the beginning.
    chan = _LoopChannel([Inbound(text="alpha: first"),
                         Inbound(text="alpha: second"),
                         Inbound(text="beta: third")])

    stopped = threading.Event()

    def _loop():
        try:
            serve(chan, cfg, interval=0.01, run=gate, log=lambda *a: None)
        except _StopLoop:
            stopped.set()

    loop = threading.Thread(target=_loop, daemon=True)
    loop.start()

    # All routes are acknowledged immediately; queueing is not refusal.
    assert _until(lambda: sum("accepted" in t for t in chan.outbox) == 3), chan.outbox
    assert "alpha · accepted: first" in chan.outbox
    assert "alpha · accepted: second" in chan.outbox

    # Beta finishes while alpha remains blocked.
    assert _until(lambda: "third" in gate.snapshot()[1]), gate.snapshot()
    started, finished = gate.snapshot()
    assert "first" in started and "second" not in started, (
        f"second started while first was running; project serialization broke: {started}")
    assert "first" not in finished

    gate.gates["first"].set()                      # Releasing first now allows second.
    assert _until(lambda: "second" in gate.snapshot()[1]), gate.snapshot()
    started, finished = gate.snapshot()
    assert started.index("first") < started.index("second")
    assert finished.index("first") < finished.index("second")
    assert stopped.wait(5) and not (loop.join(5) or loop.is_alive())
    # Alpha responses reach the operator in the same order.
    echoes = [t for t in chan.outbox if t.startswith("alpha · complete")]
    assert echoes == ["alpha · complete: first", "alpha · complete: second"]
