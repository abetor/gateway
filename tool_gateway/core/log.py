"""Machine-readable audit log for all gateway activity under ``<home>/log``.

There are three JSONL streams, one file per stream:

``inbound.jsonl``
    Every channel message and webhook request, including refusals.
``dispatch.jsonl``
    Every tool invocation, including its command and exit status.
``delivery.jsonl``
    Every delivery decision, including sent and suppressed messages and their
    text, so gate decisions remain explainable.

Each append writes one event per line. With one writer per process, POSIX append
does not split lines. Markdown is not used as runtime state.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Callable

STREAMS = ("inbound", "dispatch", "delivery")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def append(home: str | Path, stream: str, **row) -> None:
    """Append an event to ``<home>/log/<stream>.jsonl`` and add its timestamp.

    An unknown stream is an error so a typo cannot create an orphan file.
    """
    if stream not in STREAMS:
        raise ValueError(f"unknown log stream {stream!r} (available: {STREAMS})")
    path = Path(home) / "log" / f"{stream}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _now(), **row}, ensure_ascii=False) + "\n")


def bind(home: str | Path) -> Callable:
    """Bind ``home`` into an event callback so channels need not manage paths."""
    def events(stream: str, **row) -> None:
        append(home, stream, **row)
    return events
