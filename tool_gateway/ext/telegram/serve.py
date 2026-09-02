"""Telegram loop: poll, parse, enqueue by project, dispatch, then send.

Events enter the gateway's inbound, dispatch, and delivery audit streams.

Each project has its own queue and worker thread. A long turn in one project
does not block another, while turns within a project remain strictly serial so
replies cannot interleave with a running session. ``once=True`` performs one
inline pass without threads or acknowledgements for tests and debugging. It is
not a scheduling mode because overlapping instances would break serialization
and compete for ``getUpdates``.

After acknowledging a route, the loop must not lose it silently. Worker threads
survive turn failures and report them to the operator. A state file records
accepted but unfinished routes; after a crash, the next process reports them
without automatic replay because a turn may have partially succeeded. Poll and
send failures are logged rather than swallowed.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Mapping, Optional

from ...core.channel import Channel, Inbound
from ...core.config import EventTargetCfg, TelegramCfg
from .route import Route, dispatch, parse_route


def _stdout(msg: str) -> None:
    """Write and flush loop output so redirected background logs stay current.

    Without flushing, block buffering can hide hours of status and discard it
    on termination. The redirected file may be the operator's only view.
    """
    print(msg, flush=True)


def _cut(text: str, cap: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= cap else text[:cap].rsplit(" ", 1)[0] + "…"


class _Pending:
    """Fail-soft journal of accepted but unfinished routes.

    A write failure does not abort the turn. An empty path disables persistence
    for hermetic tests.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._items: list = []

    def leftover(self) -> list:
        if not self.path:
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f) or []
        except (OSError, ValueError):
            return []

    def _flush(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._items, f, ensure_ascii=False)
        except OSError:
            pass

    def add(self, route: Route) -> None:
        with self._lock:
            self._items.append(asdict(route))
            self._flush()

    def done(self, route: Route) -> None:
        with self._lock:
            try:
                self._items.remove(asdict(route))
            except ValueError:
                pass
            self._flush()

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self._flush()


def serve(channel: Channel, cfg: TelegramCfg, *, interval: float = 2.0, once: bool = False,
          run: Optional[Callable] = None, log: Callable = _stdout, state_dir: str = "",
          events: Optional[Callable] = None, home: str | Path | None = None,
          targets: Mapping[str, EventTargetCfg] | None = None,
          target_run: Optional[Callable] = None) -> int:
    log(f"gateway watch: channel={channel.name}; projects={', '.join(sorted(cfg.projects))}; "
        f"polling (Ctrl-C to stop)")
    emit = events or (lambda stream, **row: None)
    pending = _Pending(os.path.join(state_dir, "watch-pending.json") if state_dir else "")

    def _send(text: str) -> None:
        ok = channel.send(text)
        emit("delivery", channel=channel.name, source="watch", text=text, sent=ok)
        if not ok:
            log(f"gateway watch: send failed: {text[:80]!r}")

    left = pending.leftover()
    if left:                                       # Report prior leftovers without blind replay.
        note = (f"gateway: restart found unfinished routes ({len(left)}): "
                + "; ".join(_cut(f"{it.get('project', '?')}: {it.get('text', '')}") for it in left[:5])
                + "; resend only what is safe because a turn may have partially succeeded")
        log(note)
        _send(note)
        pending.clear()

    def _generic(route: Route, inbound: Inbound) -> str:
        from ..event.dispatch import EventError, dispatch_event, telegram_event

        target_name = cfg.projects[route.project].target
        target = (targets or {}).get(target_name)
        if target is None or home is None:
            emit("dispatch", channel="telegram", project=route.project,
                 kind=route.kind, target=target_name, exit=1)
            return f"{route.project} · gateway: event target unavailable"
        try:
            event = telegram_event(route, inbound, target=target_name)
            if target_run is None:
                result = dispatch_event(Path(home), event, target)
            else:
                result = dispatch_event(Path(home), event, target, run=target_run)
        except EventError as error:
            emit("dispatch", channel="telegram", project=route.project,
                 kind=route.kind, target=target_name, exit=1, error=error.code)
            return f"{route.project} · gateway: event target rejected the event"
        receipt = result.response.get("receipt")
        attempts = receipt.get("attempts") if isinstance(receipt, dict) else None
        emit("dispatch", channel="telegram", project=route.project,
             kind=route.kind, target=target_name, exit=result.exit_code,
             attempts=attempts, event_id=event["event_id"])
        if result.exit_code == 0:
            return f"{route.project} · gateway: event delivered"
        if result.exit_code == 111:
            return f"{route.project} · gateway: delivery is temporarily unconfirmed"
        return f"{route.project} · gateway: event target rejected the event"

    def _handle(route: Route, inbound: Inbound) -> None:
        """Run one complete turn and report every failure without killing the worker."""
        try:
            if cfg.projects[route.project].target == "rig":
                reply = dispatch(route, cfg, run=run, events=events)
            else:
                reply = _generic(route, inbound)
        except Exception as e:  # noqa: BLE001 - a dead worker would become a project black hole.
            if cfg.projects[route.project].target == "rig":
                # Preserve detailed diagnostics for the operator-facing session target.
                reply = f"{route.project} · gateway: turn failed - {type(e).__name__}: {e}"
            else:
                reply = f"{route.project} · gateway: event target failed"
        if reply:                                  # Empty means the flow already sent its final notice.
            _send(reply)
        pending.done(route)

    queues: dict = {}

    def _worker(q: "queue.Queue") -> None:
        while True:
            route, inbound = q.get()
            _handle(route, inbound)
            q.task_done()

    def _enqueue(route: Route, inbound: Inbound) -> None:
        q = queues.get(route.project)
        if q is None:
            q = queues[route.project] = queue.Queue()
            threading.Thread(target=_worker, args=(q,), daemon=True,
                             name=f"watch-{route.project}").start()
        q.put((route, inbound))

    handled = 0
    while True:
        msgs = channel.poll()
        err = getattr(channel, "last_error", "")
        if err:                                    # Keep 409 and expired-token failures visible.
            log(f"gateway watch: channel error: {err}")
            channel.last_error = ""
        for msg in msgs:
            log(f"gateway watch: inbound: {msg.text[:80]!r}")
            emit("inbound", channel=channel.name, text=msg.text, sender=msg.sender)
            handled += 1
            route = parse_route(msg, cfg)
            if isinstance(route, str):             # Refusal text: answer without invoking the CLI.
                _send(route)
                continue
            if once:
                _handle(route, msg)
                continue
            if route.kind in ("task", "reply"):    # Acknowledge a long turn before dispatch.
                _send(f"{route.project} · accepted: {_cut(route.text)}")
            pending.add(route)
            _enqueue(route, msg)
        if once:
            return handled
        time.sleep(interval)
