"""Deliver version 2 envelopes through Telegram.

Rendering, deduplication, availability windows, queues, and live edits share one
path. Plain text and legacy flags first become the same envelope through
``ext.notice.envelope.from_legacy``. All runtime state stays under
``<home>/state`` in ``dedup.json``, ``live/``, and ``queue/``.
"""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

from ...core import EXIT_FAIL, EXIT_OK, EXIT_TRANSIENT
from ...core.channel import make_channel
from ...core.config import Config
from ...core.log import bind
from ...shared.fs import atomic_write
from ..notice import queue as notice_queue
from ..notice.availability import parse_grid, state_at
from ..notice.envelope import (
    LEGACY_KINDS,
    Envelope,
    EnvelopeError,
    from_flat,
    from_legacy,
    validate,
)
from ..notice.render import MAX_TEXT, render

KINDS = LEGACY_KINDS
PARSE_MODE = "HTML"
DEDUP_KEEP_SECONDS = 24 * 60 * 60   # At least as long as every default class window.
EDIT_INTERVAL_SECONDS = 3
LIVE_SECONDS = 24 * 60 * 60
CLOSE_SUFFIX = "\n-> final result below"
DRAIN_LIMIT = 20


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    slug = re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE).strip("-_")
    if slug:
        return slug[:80]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _live_path(home: str | Path, source: str, subject: str) -> Path:
    readable = f"{_slug(source)}__{_slug(subject)}"
    digest = hashlib.sha256(f"{source}|{subject}".encode("utf-8")).hexdigest()[:10]
    return Path(home) / "state" / "live" / f"{readable}-{digest}.json"


def _dedup_path(home: str | Path) -> Path:
    return Path(home) / "state" / "dedup.json"


def _read_json(path: Path) -> object:
    try:
        if path.stat().st_size > 1024 * 1024:
            return None
        return json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None


def _load_live(path: Path) -> dict | None:
    raw = _read_json(path)
    if not isinstance(raw, dict) or set(raw) != {"message_id", "sent_at", "text_hash"}:
        return None
    if type(raw["message_id"]) is not int or raw["message_id"] <= 0:
        return None
    if type(raw["sent_at"]) not in {int, float}:
        return None
    if not isinstance(raw["text_hash"], str) or re.fullmatch(r"[0-9a-f]{64}", raw["text_hash"]) is None:
        return None
    return raw


def _write_json(path: Path, payload: object) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _load_dedup(path: Path, now: float) -> dict[str, float]:
    raw = _read_json(path)
    if not isinstance(raw, dict):
        return {}
    entries: dict[str, float] = {}
    for key, value in raw.items():
        if (isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)
                and type(value) in {int, float} and now - float(value) <= DEDUP_KEEP_SECONDS):
            entries[key] = float(value)
    return entries


def _dedup_key(env: Envelope) -> str:
    raw = json.dumps(env.dedupe_material(), ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _message_not_modified(channel) -> bool:
    return (getattr(channel, "last_error_code", None) == 400
            and "message is not modified" in
            str(getattr(channel, "last_error", "")).casefold())


@contextmanager
def _state_lock(home: str | Path):
    """Serialize dedup, live, and queue state across concurrent CLI processes."""
    state = Path(home) / "state"
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state / ".deliver.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _reverse_log_rows(path: Path):
    """Read JSONL backward without loading the growing delivery log at once."""
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            position = source.tell()
            remainder = b""
            while position:
                size = min(64 * 1024, position)
                position -= size
                source.seek(position)
                chunks = (source.read(size) + remainder).split(b"\n")
                remainder = chunks[0]
                for line in reversed(chunks[1:]):
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except (UnicodeError, ValueError, RecursionError):
                        continue
                    if isinstance(row, dict):
                        yield row
            if remainder:
                try:
                    row = json.loads(remainder)
                except (UnicodeError, ValueError, RecursionError):
                    return
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def _logged_live_text(home: str | Path, text_hash: str) -> str:
    for row in _reverse_log_rows(Path(home) / "log" / "delivery.jsonl"):
        if row.get("sent") is True and row.get("text_hash") == text_hash:
            text = row.get("text")
            return text if isinstance(text, str) else ""
    return ""


def _row(channel_name: str, env: Envelope, text: str, text_hash: str) -> dict:
    """Build a delivery-log row containing legacy fields and the v2 envelope.

    Keeping both schemas prevents historical readers from breaking at the
    contract boundary.
    """
    row = {
        "channel": channel_name,
        "source": "deliver",
        "text": text,
        "kind": env.cls,
        "sent": False,
        "text_hash": text_hash,
    }
    if not env.flat:
        row.update(
            notice_source=env.source, subject=env.about, title=env.title,
            lines=list(env.lines), **{"from": env.source}, about=env.about,
            **{"class": env.cls}, name=env.name, action=env.action,
            urgency=env.urgency, live=env.live,
        )
    return row


def _local_now(timestamp: float) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(timestamp).astimezone()


def _deferred_state(cfg: Config, env: Envelope, timestamp: float) -> str:
    """Return the grid state that defers this envelope, or empty to send."""
    policy = cfg.notify.classes.get(env.cls, {})
    if not policy.get("defer", True) or env.urgency == "high" or not cfg.notify.availability:
        return ""
    state = state_at(parse_grid(list(cfg.notify.availability)), _local_now(timestamp))
    return state if state in cfg.notify.defer_states else ""


class _Sender:
    """One envelope send, including live edits and live-message closure."""

    def __init__(self, home, chan, emit, clock, sleeper, channel_name: str):
        self.home, self.chan, self.emit = home, chan, emit
        self.clock, self.sleeper, self.channel_name = clock, sleeper, channel_name

    def send(self, env: Envelope, text: str, timestamp: float) -> tuple[bool, str, dict]:
        """Return ``(ok, mode, row)`` with ``sent`` and ``reason`` populated.

        The caller is responsible for writing successful rows.
        """
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        row = _row(self.channel_name, env, text, text_hash)
        parse_mode = "" if env.flat else PARSE_MODE
        live_path = _live_path(self.home, env.source, env.about) if (env.source and env.about and not env.flat) else None
        live = _load_live(live_path) if live_path is not None else None
        live_age = timestamp - float(live["sent_at"]) if live is not None else None
        live_fresh = live is not None and live_age < LIVE_SECONDS

        mode, edit_error = "new", ""
        if env.live and live_fresh:
            if live["text_hash"] == text_hash:
                return True, "same", {**row, "sent": False, "reason": "same"}
            delay = max(0.0, EDIT_INTERVAL_SECONDS - max(0.0, live_age))
            if delay:
                self.sleeper(delay)
                timestamp = max(float(self.clock()), timestamp + delay)
            ok = self.chan.edit(live["message_id"], text, parse_mode=parse_mode)
            if ok or _message_not_modified(self.chan):
                ok, mode, message_id = True, "edit", live["message_id"]
            else:
                edit_error = getattr(self.chan, "last_error", "") or "editMessageText failed"
                ok = self.chan.send(text, parse_mode=parse_mode)
                message_id = getattr(self.chan, "last_message_id", None)
        else:
            ok = self.chan.send(text, parse_mode=parse_mode)
            message_id = getattr(self.chan, "last_message_id", None)

        if not ok:
            detail = getattr(self.chan, "last_error", "") or "send failed"
            failed = {**row, "sent": False, "reason": detail}
            if edit_error:
                failed["edit_error"] = edit_error
            return False, mode, failed

        if env.live and live_path is not None and isinstance(message_id, int):
            _write_json(live_path, {"message_id": message_id, "sent_at": timestamp,
                                    "text_hash": text_hash})
        elif not env.live and live_path is not None and live is not None and env.cls not in {"ack", "status"}:
            # A non-live message for the same operation is a milestone or
            # terminal result. Close the live message by pointing to it, while
            # acknowledgements and status snapshots leave live state untouched.
            old_text = _logged_live_text(self.home, live["text_hash"])
            close_text = (old_text + CLOSE_SUFFIX) if old_text else "-> final result below"
            close_ok = self.chan.edit(live["message_id"], close_text, parse_mode=PARSE_MODE)
            if not close_ok and not _message_not_modified(self.chan):
                self.emit("delivery", **{
                    **row, "text": close_text,
                    "text_hash": hashlib.sha256(close_text.encode("utf-8")).hexdigest(),
                    "sent": False, "reason": "close_edit_failed",
                    "edit_error": getattr(self.chan, "last_error", "") or "editMessageText failed",
                    "mode": "close",
                })
            try:
                live_path.unlink()
            except FileNotFoundError:
                pass
        logged = {**row, "sent": True, "mode": mode}
        if edit_error:
            logged["edit_error"] = edit_error
        return True, mode, logged


def _channel(cfg: Config, channel_name: str, http):
    if channel_name != "telegram":
        raise ValueError(f"channel {channel_name!r} cannot deliver messages (Telegram only)")
    if cfg.telegram is None:
        raise ValueError("[channels.telegram] is not configured in gateway.toml")
    return make_channel("telegram", http=http, token_env=cfg.telegram.token_env,
                        chat_id_env=cfg.telegram.chat_id_env)


def _drain_locked(home, cfg: Config, sender: _Sender, timestamp: float, *,
                  limit: int) -> dict[str, int]:
    counts = {"sent": 0, "kept": 0, "dropped": 0}
    for path in notice_queue.pending(home)[:limit]:
        loaded = notice_queue.load(path)
        if loaded is None:
            sender.emit("delivery", channel=sender.channel_name, source="deliver",
                        text="", kind="queue", sent=False, reason=f"queue: corrupt file {path.name}")
            path.unlink(missing_ok=True)
            counts["dropped"] += 1
            continue
        env, meta = loaded
        try:
            env = validate(env, cfg.notify.classes)
            text = render(env, home=home)
        except (EnvelopeError, ValueError) as error:
            sender.emit("delivery", **{**_row(sender.channel_name, env, "", ""),
                                       "sent": False, "reason": f"queue: {error}"})
            path.unlink(missing_ok=True)
            counts["dropped"] += 1
            continue
        age = timestamp - float(meta["queued_at"])
        reason = str(meta.get("reason", ""))
        deferred = reason.startswith("deferred")
        max_age = cfg.notify.defer_max_age_seconds if deferred else cfg.notify.retry_max_age_seconds
        if age > max_age:
            sender.emit("delivery", **{**_row(sender.channel_name, env, text, ""),
                                       "sent": False, "reason": f"dropped:age {int(age)}s ({reason})"})
            path.unlink(missing_ok=True)
            counts["dropped"] += 1
            continue
        if deferred and _deferred_state(cfg, env, timestamp):
            counts["kept"] += 1
            continue
        ok, _mode, row = sender.send(env, text, timestamp)
        if ok:
            if row.get("sent"):
                sender.emit("delivery", **{**row, "queued": reason})
            path.unlink(missing_ok=True)
            counts["sent"] += 1
            continue
        permanent = bool(getattr(sender.chan, "last_permanent", False))
        if permanent:
            sender.emit("delivery", **{**row, "reason": "platform refusal: " + str(row.get("reason", ""))})
            path.unlink(missing_ok=True)
            counts["dropped"] += 1
        else:
            sender.emit("delivery", **{**row, "reason": "retry: " + str(row.get("reason", ""))})
            notice_queue.bump(path, meta)
            counts["kept"] += 1
    return counts


def drain(home, cfg: Config, *, channel_name: str = "telegram", http=None,
          events: Optional[Callable] = None, now: Optional[Callable[[], float]] = None,
          pause: Optional[Callable[[float], None]] = None, limit: int = 200) -> int:
    """Send deferred and retryable notices when the current window allows.

    Return 0 except for configuration or credential failures, which return 1.
    A non-empty queue does not ask an external caller to retry.
    """
    emit = events or bind(home)
    try:
        chan = _channel(cfg, channel_name, http)
    except ValueError as error:
        print(f"drain: {error}", file=sys.stderr)
        return EXIT_FAIL
    timestamp = float((now or time.time)())
    sender = _Sender(home, chan, emit, now or time.time, pause or time.sleep, channel_name)
    try:
        with _state_lock(home):
            counts = _drain_locked(home, cfg, sender, timestamp, limit=limit)
    except OSError as error:
        print(f"drain: state unavailable: {error}", file=sys.stderr)
        return EXIT_FAIL
    print(f"drain: sent {counts['sent']}, kept {counts['kept']}, "
          f"dropped {counts['dropped']}")
    return EXIT_OK


def deliver_envelope(home, cfg: Config, env: Envelope, *, channel_name: str = "telegram",
                     http=None, events: Optional[Callable] = None,
                     now: Optional[Callable[[], float]] = None,
                     pause: Optional[Callable[[float], None]] = None) -> int:
    """Deliver one envelope.

    Exit 0 means sent, suppressed by a gate, or queued for a window or retry.
    Exit 111 means a transient delivery failure could not be queued. Exit 1
    means invalid configuration, credentials, envelope, or permanent refusal.
    """
    emit = events or bind(home)
    clock = now or time.time
    sleeper = pause or time.sleep

    def refuse(reason: str, code: int, text: str = "") -> int:
        emit("delivery", **{**_row(channel_name, env, text or env.title, ""),
                            "sent": False, "reason": reason})
        print(f"deliver: {reason}", file=sys.stderr)
        return code

    try:
        env = validate(env, cfg.notify.classes)
        wire_text = render(env, home=home)
    except (EnvelopeError, ValueError) as error:
        return refuse(str(error), EXIT_FAIL)
    try:
        chan = _channel(cfg, channel_name, http)
    except ValueError as error:
        return refuse(str(error), EXIT_FAIL, wire_text)

    timestamp = float(clock())
    sender = _Sender(home, chan, emit, clock, sleeper, channel_name)
    text_hash = hashlib.sha256(wire_text.encode("utf-8")).hexdigest()
    row = _row(channel_name, env, wire_text, text_hash)
    try:
        with _state_lock(home):
            if notice_queue.pending(home):
                _drain_locked(home, cfg, sender, timestamp, limit=DRAIN_LIMIT)
            dedup_path = _dedup_path(home)
            dedup = _load_dedup(dedup_path, timestamp)
            key = _dedup_key(env)
            window = int(cfg.notify.classes.get(env.cls, {}).get("dedupe_seconds", 0))
            previous = dedup.get(key)
            if window and previous is not None and timestamp - previous < window:
                _write_json(dedup_path, dedup)
                emit("delivery", **{**row, "sent": False, "reason": "dup"})
                print("deliver: delivered (dup)")
                return EXIT_OK

            state = _deferred_state(cfg, env, timestamp)
            if state:
                path = notice_queue.enqueue(home, env, reason=f"deferred:{state}", now=timestamp)
                dedup[key] = timestamp
                _write_json(dedup_path, dedup)
                emit("delivery", **{**row, "sent": False, "reason": f"deferred:{state}",
                                    "queued": path.name})
                print(f"deliver: deferred ({state}), pending drain")
                return EXIT_OK

            ok, mode, logged = sender.send(env, wire_text, timestamp)
            if ok:
                if mode == "same":
                    _write_json(dedup_path, dedup)
                    emit("delivery", **logged)
                    print("deliver: delivered (dup)")
                    return EXIT_OK
                dedup[key] = timestamp
                _write_json(dedup_path, dedup)
                emit("delivery", **logged)
                print(f"deliver: delivered ({'edit' if mode == 'edit' else 'new'})")
                return EXIT_OK

            _write_json(dedup_path, dedup)
            detail = str(logged.get("reason", "")) or "send failed"
            if getattr(chan, "last_permanent", False):
                emit("delivery", **{**logged, "reason":
                                    "permanent platform refusal (credentials/recipient): " + detail})
                print(f"deliver: not delivered; permanent platform refusal ({detail})",
                      file=sys.stderr)
                return EXIT_FAIL
            try:
                path = notice_queue.enqueue(home, env, reason="retry", now=timestamp, attempts=1)
            except OSError as error:
                emit("delivery", **{**logged, "reason": f"network/platform: {detail}; queue: {error}"})
                print(f"deliver: not delivered (network/platform); queue unavailable ({detail})",
                      file=sys.stderr)
                return EXIT_TRANSIENT
            emit("delivery", **{**logged, "reason": f"retry: {detail}", "queued": path.name})
            print(f"deliver: not delivered (network/platform), queued for retry ({detail})",
                  file=sys.stderr)
            return EXIT_OK
    except OSError as error:
        emit("delivery", **{**row, "sent": False, "reason": f"state: {error}"})
        print(f"deliver: state unavailable: {error}", file=sys.stderr)
        return EXIT_FAIL


def deliver(home, cfg: Config, text: str, *, channel_name: str = "telegram",
            kind: str = "info", source: str = "", subject: str = "",
            title: str = "", lines: str = "", structured: bool | None = None,
            http=None, events: Optional[Callable] = None,
            now: Optional[Callable[[], float]] = None,
            pause: Optional[Callable[[float], None]] = None) -> int:
    """Accept plain text or legacy structured flags through one compatibility path.

    Every input becomes a version 2 envelope and continues through
    ``deliver_envelope``.
    """
    structured = (bool(source or subject or title or lines or kind != "info")
                  if structured is None else structured)
    emit = events or bind(home)
    try:
        if structured:
            missing = [name for name, value in (("source", source), ("subject", subject),
                                                 ("title", title)) if not str(value).strip()]
            if missing:
                raise EnvelopeError("structured notice requires " + ", ".join(missing))
            env = from_legacy(kind=kind, source=source.strip(), subject=subject.strip(),
                              title=title.strip(), lines=lines)
        else:
            env = from_flat(text)
    except EnvelopeError as error:
        emit("delivery", channel=channel_name, source="deliver",
             text=(title or text) if structured else text, kind=kind, sent=False,
             reason=str(error))
        print(f"deliver: {error}", file=sys.stderr)
        return EXIT_FAIL
    return deliver_envelope(home, cfg, env, channel_name=channel_name, http=http,
                            events=events, now=now, pause=pause)


__all__ = [
    "KINDS", "PARSE_MODE", "MAX_TEXT", "CLOSE_SUFFIX", "LIVE_SECONDS",
    "deliver", "deliver_envelope", "drain", "_live_path",
]
