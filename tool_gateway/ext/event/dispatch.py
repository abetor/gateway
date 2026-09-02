"""Bounded, replay-safe dispatch to a configured argv target.

A target is a subprocess contract, not a shell string. It receives one
canonical JSON event on stdin and returns one bounded JSON receipt on stdout.
Its diagnostics are untrusted and are never copied to gateway responses or logs.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time
from typing import TYPE_CHECKING, Callable, Iterator, Mapping
import unicodedata

from ...core import EXIT_FAIL, EXIT_OK, EXIT_TRANSIENT
from ...core.channel import Inbound
from ...core.config import EventTargetCfg
from ...shared.fs import atomic_write

if TYPE_CHECKING:
    from ..telegram.route import Route

EVENT_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 64 * 1024
MAX_TARGET_OUTPUT_BYTES = 64 * 1024
MAX_RECEIPT_BYTES = 64 * 1024

_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "event_id",
        "dedupe_key",
        "target",
        "source",
        "kind",
        "text",
        "project",
        "ref",
    }
)
_TARGET_RECEIPT_KEYS = frozenset(
    {"schema_version", "event_id", "dedupe_key", "status", "effect_sha256"}
)
_PUBLIC_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "event_id",
        "dedupe_key",
        "status",
        "effect_sha256",
        "attempts",
    }
)
_STORED_RECEIPT_KEYS = _PUBLIC_RECEIPT_KEYS | {"event_sha256", "target"}
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_EVENT_ID = re.compile(r"^event_[a-z0-9][a-z0-9_-]{0,57}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_SECRET_ARG = re.compile(
    r"(?i)(?:^--?(?:api[-_]?key|authorization|password|secret|token)(?:=|$)|"
    r"\b(?:bearer|basic)\s+\S+|\bsk-[a-z0-9_-]{8,})"
)
_ZERO_HASH = "0" * 64
_SAFE_BASE_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR")
_PRIVATE_MARKERS = ("/users/", "/home/", "file:", "c:\\users\\")
_CREDENTIAL_LABEL = re.compile(
    r"(?i)(?:^|[\s,;|{\[(])(?:authorization|api[_ -]?key|access[_ -]?token|"
    r"refresh[_ -]?token|client[_ -]?secret|token|secret|password|passwd|"
    r"cookie|credential|chat[_ -]?id)\s*[:=]"
)
_AUTH_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+\S+")
_COMMON_SECRET = re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}")


class EventError(ValueError):
    """Event, receipt, or transport state violates the public contract."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TargetProcessResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_too_large: bool = False


@dataclass(frozen=True)
class DispatchResult:
    exit_code: int
    response: dict[str, object]
    diagnostic: str


def _fail(code: str) -> None:
    raise EventError(code)


def _has_control(value: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    )


def _text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        _fail("invalid_" + label)
    try:
        raw = value.encode("utf-8")
    except UnicodeError:
        _fail("invalid_" + label)
    if (
        not value
        or value.strip() != value
        or value != unicodedata.normalize("NFC", value)
        or len(raw) > maximum
        or _has_control(value)
    ):
        _fail("invalid_" + label)
    return value


def _name(value: object, label: str) -> str:
    checked = _text(value, label, maximum=64)
    if _NAME.fullmatch(checked) is None:
        _fail("invalid_" + label)
    return checked


def _event_id(value: object) -> str:
    checked = _text(value, "event_id", maximum=64)
    if _EVENT_ID.fullmatch(checked) is None:
        _fail("invalid_event_id")
    return checked


def _dedupe_key(value: object) -> str:
    checked = _text(value, "dedupe_key", maximum=71)
    if not checked.startswith("sha256:") or _SHA256.fullmatch(checked[7:]) is None:
        _fail("invalid_dedupe_key")
    return checked


def _hash(value: object, label: str) -> str:
    checked = _text(value, label, maximum=64)
    if _SHA256.fullmatch(checked) is None:
        _fail("invalid_" + label)
    return checked


def _safe_message(value: object) -> str:
    checked = _text(value, "text", maximum=4096)
    folded = checked.casefold()
    if (
        any(marker in folded for marker in _PRIVATE_MARKERS)
        or _CREDENTIAL_LABEL.search(checked)
        or _AUTH_VALUE.search(checked)
        or _COMMON_SECRET.search(checked)
    ):
        _fail("unsafe_text")
    return checked


def _canonical_bytes(payload: Mapping[str, object], *, maximum: int = MAX_EVENT_BYTES) -> bytes:
    try:
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        _fail("invalid_json")
    if len(rendered) > maximum:
        _fail("input_too_large")
    return rendered


def _object_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_field")
        result[key] = value
    return result


def decode_event(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_EVENT_BYTES:
        _fail("input_too_large")
    try:
        payload = json.loads(raw, object_pairs_hook=_object_no_duplicates)
    except EventError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _fail("invalid_json")
    return validate_event(payload)


def validate_event(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping) or set(payload) != _EVENT_KEYS:
        _fail("invalid_event_fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        _fail("unsupported_event_schema")
    event_id = _event_id(payload["event_id"])
    dedupe_key = _dedupe_key(payload["dedupe_key"])
    if event_id != "event_" + dedupe_key[7:65]:
        _fail("event_identity_mismatch")
    target = _name(payload["target"], "target")
    source = payload["source"]
    if source not in {"runtime", "telegram"}:
        _fail("invalid_source")
    kind = _name(payload["kind"], "kind")
    text = _safe_message(payload["text"])
    project_value = payload["project"]
    project = None if project_value is None else _name(project_value, "project")
    ref = payload["ref"]
    if ref is not None and (type(ref) is not int or not 0 < ref <= (2**63 - 1)):
        _fail("invalid_ref")
    if source == "runtime" and (project is not None or ref is not None):
        _fail("invalid_runtime_context")
    if source == "telegram" and (project is None or kind != "task" or ref is not None):
        _fail("invalid_telegram_context")
    checked = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "dedupe_key": dedupe_key,
        "target": target,
        "source": source,
        "kind": kind,
        "text": text,
        "project": project,
        "ref": ref,
    }
    _canonical_bytes(checked)
    return checked


def telegram_event(route: Route, inbound: Inbound, *, target: str) -> dict[str, object]:
    """Build a stable event without forwarding sender IDs or raw platform data."""

    if route.kind != "task" or route.ref is not None:
        _fail("non_session_target_route")

    update_id: int | None = None
    if isinstance(inbound.raw, Mapping):
        raw_update_id = inbound.raw.get("update_id")
        if type(raw_update_id) is int and 0 <= raw_update_id <= (2**63 - 1):
            update_id = raw_update_id
    identity = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "source": "telegram",
        "update_id": update_id,
        "project": route.project,
        "kind": route.kind,
        "text": route.text,
        "ref": route.ref,
    }
    digest = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
    return validate_event(
        {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": "event_" + digest[:58],
            "dedupe_key": "sha256:" + digest,
            "target": target,
            "source": "telegram",
            "kind": route.kind,
            "text": route.text,
            "project": route.project,
            "ref": route.ref,
        }
    )


def _target_receipt(raw: bytes, event: Mapping[str, object]) -> dict[str, object]:
    if len(raw) > MAX_TARGET_OUTPUT_BYTES:
        _fail("target_output_too_large")
    try:
        payload = json.loads(raw, object_pairs_hook=_object_no_duplicates)
    except EventError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _fail("invalid_target_receipt")
    if not isinstance(payload, Mapping) or set(payload) != _TARGET_RECEIPT_KEYS:
        _fail("invalid_target_receipt")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        _fail("invalid_target_receipt")
    if payload["event_id"] != event["event_id"]:
        _fail("target_receipt_identity_mismatch")
    if payload["dedupe_key"] != event["dedupe_key"]:
        _fail("target_receipt_identity_mismatch")
    if payload["status"] not in {"delivered", "failed"}:
        _fail("invalid_target_receipt")
    effect = _hash(payload["effect_sha256"], "effect_sha256")
    if payload["status"] == "delivered" and effect == _ZERO_HASH:
        _fail("invalid_target_receipt")
    checked = dict(payload)
    checked["effect_sha256"] = effect
    _canonical_bytes(checked, maximum=MAX_RECEIPT_BYTES)
    return checked


def _public_receipt(stored: Mapping[str, object]) -> dict[str, object]:
    return {key: stored[key] for key in sorted(_PUBLIC_RECEIPT_KEYS)}


def _validate_stored(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping) or set(payload) != _STORED_RECEIPT_KEYS:
        _fail("invalid_receipt_state")
    event_id = _event_id(payload["event_id"])
    dedupe_key = _dedupe_key(payload["dedupe_key"])
    if event_id != "event_" + dedupe_key[7:65]:
        _fail("invalid_receipt_state")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        _fail("invalid_receipt_state")
    if payload["status"] not in {"delivered", "failed", "retryable"}:
        _fail("invalid_receipt_state")
    _hash(payload["effect_sha256"], "effect_sha256")
    _hash(payload["event_sha256"], "event_sha256")
    _name(payload["target"], "target")
    attempts = payload["attempts"]
    if type(attempts) is not int or not 0 <= attempts <= (2**31 - 1):
        _fail("invalid_receipt_state")
    checked = dict(payload)
    _canonical_bytes(checked, maximum=MAX_RECEIPT_BYTES)
    return checked


def _read_regular_bounded(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError:
        _fail("receipt_read_failed")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            _fail("invalid_receipt_state")
        data = bytearray()
        while len(data) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > maximum:
            _fail("invalid_receipt_state")
        return bytes(data)
    finally:
        os.close(fd)


def _receipt_path(home: Path, event_id: str) -> Path:
    return home / "state" / "event-receipts" / f"{event_id}.json"


def _load_stored(home: Path, event_id: str) -> dict[str, object] | None:
    try:
        raw = _read_regular_bounded(_receipt_path(home, event_id), MAX_RECEIPT_BYTES)
    except FileNotFoundError:
        return None
    try:
        payload = json.loads(raw, object_pairs_hook=_object_no_duplicates)
    except EventError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _fail("invalid_receipt_state")
    stored = _validate_stored(payload)
    if stored["event_id"] != event_id:
        _fail("invalid_receipt_state")
    return stored


def _write_stored(home: Path, stored: Mapping[str, object]) -> None:
    raw = _canonical_bytes(stored, maximum=MAX_RECEIPT_BYTES) + b"\n"
    try:
        atomic_write(_receipt_path(home, str(stored["event_id"])), raw)
    except OSError:
        _fail("receipt_write_failed")


@contextmanager
def _receipt_lock(home: Path) -> Iterator[None]:
    directory = home / "state" / "event-receipts"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        _fail("receipt_lock_failed")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            _fail("receipt_lock_failed")
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _target_env(target: EventTargetCfg) -> dict[str, str]:
    missing = [name for name in target.env if not os.environ.get(name)]
    if missing:
        _fail("target_env_missing")
    result = {name: os.environ[name] for name in _SAFE_BASE_ENV if name in os.environ}
    result.update({name: os.environ[name] for name in target.env})
    return result


def _valid_argv_item(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        raw = value.encode("utf-8")
    except UnicodeError:
        return False
    return (
        len(raw) <= 4096
        and value == unicodedata.normalize("NFC", value)
        and not _has_control(value)
        and _SECRET_ARG.search(value) is None
    )


def _validate_target_config(target: object) -> EventTargetCfg:
    if not isinstance(target, EventTargetCfg):
        _fail("invalid_target_config")
    if (
        not isinstance(target.argv, tuple)
        or not 0 < len(target.argv) <= 32
        or not all(_valid_argv_item(item) for item in target.argv)
        or sum(len(item.encode("utf-8")) for item in target.argv) > 16 * 1024
    ):
        _fail("invalid_target_config")
    if (
        not isinstance(target.env, tuple)
        or len(target.env) > 32
        or not all(isinstance(name, str) and _ENV_NAME.fullmatch(name) for name in target.env)
        or len(set(target.env)) != len(target.env)
    ):
        _fail("invalid_target_config")
    if type(target.retries) is not int or not 0 <= target.retries <= 8:
        _fail("invalid_target_config")
    for value, minimum, maximum in (
        (target.retry_delay_seconds, 0.0, 60.0),
        (target.timeout_seconds, 0.001, 3600.0),
    ):
        if type(value) not in {int, float}:
            _fail("invalid_target_config")
        try:
            checked_value = float(value)
        except (OverflowError, ValueError):
            _fail("invalid_target_config")
        if not math.isfinite(checked_value):
            _fail("invalid_target_config")
        if not minimum <= checked_value <= maximum:
            _fail("invalid_target_config")
    return target


def run_target(
    argv: tuple[str, ...],
    *,
    stdin: bytes,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> TargetProcessResult:
    """Run without a shell and finish the parent and pipes within one monotonic deadline."""

    started = time.monotonic()
    deadline = started + timeout
    cleanup_reserve = min(0.05, timeout / 10)
    execution_deadline = deadline - cleanup_reserve
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        bufsize=0,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    streams = {
        "stdin": process.stdin,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }
    selector: selectors.BaseSelector | None = None
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    open_reads = {"stdout", "stderr"}
    stdin_offset = 0
    parent_exited_at: float | None = None
    timed_out = False
    group_killed = False

    def _close(name: str) -> None:
        stream = streams[name]
        if selector is not None:
            try:
                selector.unregister(stream)
            except (KeyError, OSError, ValueError):
                pass
        try:
            stream.close()
        except (OSError, ValueError):
            pass
        open_reads.discard(name)

    def _kill_group() -> None:
        nonlocal group_killed
        if group_killed:
            return
        group_killed = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass

    try:
        selector = selectors.DefaultSelector()
        for stream in streams.values():
            os.set_blocking(stream.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        if stdin:
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            _close("stdin")
    except Exception:
        _kill_group()
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        for name in tuple(streams):
            _close(name)
        if selector is not None:
            try:
                selector.close()
            except (OSError, ValueError):
                pass
        raise EventError("target_setup_failed") from None

    assert selector is not None

    try:
        while True:
            now = time.monotonic()
            parent_rc = process.poll()
            if parent_rc is not None and parent_exited_at is None:
                parent_exited_at = now
                _close("stdin")
            if parent_rc is not None and not open_reads:
                break
            if parent_rc is None and now >= execution_deadline:
                timed_out = True
                _kill_group()
                _close("stdin")
            elif (
                parent_exited_at is not None
                and open_reads
                and now >= min(parent_exited_at + 0.05, execution_deadline)
            ):
                # Parent reported an outcome while a descendant retained its pipes.
                timed_out = True
                _kill_group()
            if now >= deadline:
                timed_out = True
                _kill_group()
                break

            wake_at = deadline
            if parent_rc is None and not group_killed:
                wake_at = min(wake_at, execution_deadline)
            if parent_exited_at is not None and open_reads and not group_killed:
                wake_at = min(wake_at, parent_exited_at + 0.05)
            wait_seconds = max(0.0, min(0.05, wake_at - now))
            try:
                ready = selector.select(wait_seconds)
            except OSError:
                timed_out = True
                _kill_group()
                break
            for key, _ in ready:
                name = key.data
                stream = key.fileobj
                if name == "stdin":
                    try:
                        written = os.write(stream.fileno(), stdin[stdin_offset:])
                    except (BlockingIOError, BrokenPipeError, OSError):
                        _close("stdin")
                    else:
                        stdin_offset += written
                        if stdin_offset == len(stdin):
                            _close("stdin")
                    continue
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    _close(name)
                    continue
                if not chunk:
                    _close(name)
                    continue
                remaining = MAX_TARGET_OUTPUT_BYTES + 1 - len(captured[name])
                if remaining > 0:
                    captured[name].extend(chunk[:remaining])
    finally:
        if process.poll() is None or open_reads:
            _kill_group()
        # Cleanup was reserved inside the same deadline; poll reaps without blocking.
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        for name in tuple(streams):
            _close(name)
        selector.close()

    returncode = None if timed_out else process.returncode
    stdout = bytes(captured["stdout"])
    stderr = bytes(captured["stderr"])
    too_large = len(stdout) > MAX_TARGET_OUTPUT_BYTES or len(stderr) > MAX_TARGET_OUTPUT_BYTES
    return TargetProcessResult(
        returncode=returncode,
        stdout=stdout[: MAX_TARGET_OUTPUT_BYTES + 1],
        stderr=stderr[: MAX_TARGET_OUTPUT_BYTES + 1],
        timed_out=timed_out,
        output_too_large=too_large,
    )


def _response(receipt: Mapping[str, object], *, replayed: bool) -> dict[str, object]:
    public = _public_receipt(receipt)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "event_id": public["event_id"],
        "status": public["status"],
        "receipt": public,
        "replayed": replayed,
    }


def _stored_receipt(
    event: Mapping[str, object],
    *,
    status: str,
    effect_sha256: str,
    attempts: int,
) -> dict[str, object]:
    return _validate_stored(
        {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "event_id": event["event_id"],
            "dedupe_key": event["dedupe_key"],
            "status": status,
            "effect_sha256": effect_sha256,
            "attempts": attempts,
            "event_sha256": hashlib.sha256(_canonical_bytes(event)).hexdigest(),
            "target": event["target"],
        }
    )


def dispatch_event(
    home: Path,
    event: Mapping[str, object],
    target: EventTargetCfg,
    *,
    run: Callable[..., TargetProcessResult] = run_target,
    sleep: Callable[[float], None] = time.sleep,
) -> DispatchResult:
    """Deliver once per event content, retrying only explicit transient outcomes."""

    checked = validate_event(event)
    target = _validate_target_config(target)
    event_hash = hashlib.sha256(_canonical_bytes(checked)).hexdigest()
    with _receipt_lock(home):
        prior = _load_stored(home, str(checked["event_id"]))
        if prior is not None:
            if (
                prior["event_sha256"] != event_hash
                or prior["dedupe_key"] != checked["dedupe_key"]
                or prior["target"] != checked["target"]
            ):
                _fail("event_conflict")
            if prior["status"] == "delivered":
                return DispatchResult(
                    EXIT_OK, _response(prior, replayed=True), "event_replayed"
                )
            attempts = int(prior["attempts"])
        else:
            attempts = 0

        stdin = _canonical_bytes(checked) + b"\n"
        try:
            env = _target_env(target)
        except EventError:
            failed = _stored_receipt(
                checked,
                status="failed",
                effect_sha256=_ZERO_HASH,
                attempts=attempts,
            )
            _write_stored(home, failed)
            return DispatchResult(EXIT_FAIL, _response(failed, replayed=False), "target_env_missing")

        for current in range(target.retries + 1):
            attempts += 1
            if attempts > (2**31 - 1):
                _fail("attempt_counter_overflow")
            try:
                result = run(
                    target.argv,
                    stdin=stdin,
                    cwd=home,
                    env=env,
                    timeout=target.timeout_seconds,
                )
            except OSError:
                failed = _stored_receipt(
                    checked,
                    status="failed",
                    effect_sha256=_ZERO_HASH,
                    attempts=attempts,
                )
                _write_stored(home, failed)
                return DispatchResult(
                    EXIT_FAIL, _response(failed, replayed=False), "target_unavailable"
                )

            if result.output_too_large:
                failed = _stored_receipt(
                    checked,
                    status="failed",
                    effect_sha256=_ZERO_HASH,
                    attempts=attempts,
                )
                _write_stored(home, failed)
                return DispatchResult(
                    EXIT_FAIL, _response(failed, replayed=False), "target_output_too_large"
                )
            if result.returncode == EXIT_OK:
                try:
                    target_receipt = _target_receipt(result.stdout, checked)
                    if target_receipt["status"] != "delivered":
                        _fail("invalid_target_receipt")
                except EventError:
                    failed = _stored_receipt(
                        checked,
                        status="failed",
                        effect_sha256=_ZERO_HASH,
                        attempts=attempts,
                    )
                    _write_stored(home, failed)
                    return DispatchResult(
                        EXIT_FAIL, _response(failed, replayed=False), "invalid_target_receipt"
                    )
                delivered = _stored_receipt(
                    checked,
                    status="delivered",
                    effect_sha256=str(target_receipt["effect_sha256"]),
                    attempts=attempts,
                )
                _write_stored(home, delivered)
                return DispatchResult(
                    EXIT_OK, _response(delivered, replayed=False), "event_delivered"
                )
            if result.returncode == EXIT_FAIL:
                effect = _ZERO_HASH
                try:
                    target_receipt = _target_receipt(result.stdout, checked)
                    if target_receipt["status"] != "failed":
                        _fail("invalid_target_receipt")
                    effect = str(target_receipt["effect_sha256"])
                except EventError:
                    pass
                failed = _stored_receipt(
                    checked,
                    status="failed",
                    effect_sha256=effect,
                    attempts=attempts,
                )
                _write_stored(home, failed)
                return DispatchResult(
                    EXIT_FAIL, _response(failed, replayed=False), "target_failed"
                )
            transient = result.timed_out or result.returncode == EXIT_TRANSIENT
            if not transient:
                failed = _stored_receipt(
                    checked,
                    status="failed",
                    effect_sha256=_ZERO_HASH,
                    attempts=attempts,
                )
                _write_stored(home, failed)
                return DispatchResult(
                    EXIT_FAIL, _response(failed, replayed=False), "target_contract_error"
                )
            if current < target.retries and target.retry_delay_seconds:
                sleep(target.retry_delay_seconds)

        retryable = _stored_receipt(
            checked,
            status="retryable",
            effect_sha256=_ZERO_HASH,
            attempts=attempts,
        )
        _write_stored(home, retryable)
        return DispatchResult(
            EXIT_TRANSIENT, _response(retryable, replayed=False), "target_transient"
        )


def read_receipt(home: Path, event_id: str) -> dict[str, object] | None:
    """Read-only receipt lookup; unlike dispatch it never creates state directories."""

    checked_id = _event_id(event_id)
    stored = _load_stored(home, checked_id)
    return None if stored is None else _public_receipt(stored)
