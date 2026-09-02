"""Queue for notices deferred by availability windows or transient failures.

Each envelope has one file under ``<home>/state/queue/``. ``drain`` sends what
it can and records expired items in the delivery log with a reason, so nothing
disappears silently.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from ...shared.fs import atomic_write
from .envelope import Envelope, from_dict

_NAME = re.compile(r"^\d{13}-[0-9a-f]{8}\.json$")


def queue_dir(home: str | Path) -> Path:
    return Path(home) / "state" / "queue"


def enqueue(home: str | Path, env: Envelope, *, reason: str, now: float,
            attempts: int = 0) -> Path:
    payload = env.to_dict()
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8]
    path = queue_dir(home) / f"{int(now * 1000):013d}-{digest}.json"
    atomic_write(path, json.dumps({
        "envelope": payload, "reason": reason, "queued_at": now, "attempts": attempts,
    }, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def load(path: Path) -> tuple[Envelope, dict] | None:
    try:
        if path.stat().st_size > 256 * 1024:
            return None
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("envelope"), dict):
        return None
    if type(raw.get("queued_at")) not in {int, float}:
        return None
    return from_dict(raw["envelope"]), raw


def pending(home: str | Path) -> list[Path]:
    directory = queue_dir(home)
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if _NAME.fullmatch(path.name))


def bump(path: Path, meta: dict) -> None:
    meta = dict(meta)
    meta["attempts"] = int(meta.get("attempts", 0)) + 1
    meta["last_attempt"] = time.time()
    atomic_write(path, json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n")
