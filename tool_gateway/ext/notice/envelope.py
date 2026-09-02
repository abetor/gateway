"""Version 2 envelope fields, limits, class registry, and legacy compatibility.

Configuration extends the registry through ``[notify.classes.<name>]``. A new
class needs one configuration entry and a ``<home>/templates/<name>.txt`` file,
without a code change.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# The core owns the default class registry. Each class defines how long a
# duplicate key is suppressed and whether availability windows can defer it.
# Action requests are never deferred because work may be waiting for a person.
from ...core.config import DEFAULT_CLASSES  # noqa: F401 - public re-export

MAX_ABOUT = 48
MAX_TITLE = 120
MAX_LINE = 200
MAX_LINES = 40
MAX_NAME = 120
MAX_ACTION = 200
MAX_JSON_BYTES = 64 * 1024
URGENCIES = ("low", "normal", "high")
_FROM = re.compile(r"^\w[\w-]{0,31}$")   # Unicode preserves legacy hook identifiers.
_CLASS = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Legacy notice kind to v2 class; progress updates one live message.
LEGACY_KIND_CLASS = {
    "progress": "event",
    "done": "event",
    "fail": "alert",
    "stall": "alert",
    "wait": "event",
    "info": "ack",
}
LEGACY_KINDS = frozenset(LEGACY_KIND_CLASS)


class EnvelopeError(ValueError):
    """Envelope contract violation with a one-line delivery-log reason."""


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


@dataclass
class Envelope:
    source: str                     # ``from``: sending flow or tool
    about: str                      # unique operation name
    cls: str                        # registered class
    title: str
    lines: list[str] = field(default_factory=list)
    name: str = ""                  # human-readable subject name
    action: str = ""                # one requested human action
    urgency: str = "normal"
    dedupe: str = ""                # explicit key; empty derives from envelope fields
    live: bool = False              # update one live message per (from, about)
    at: str = ""                    # source ISO timestamp
    flat: bool = False              # plain text: title contains all text, no template

    def to_dict(self) -> dict:
        return {
            "from": self.source, "about": self.about, "class": self.cls,
            "title": self.title, "lines": list(self.lines), "name": self.name,
            "action": self.action, "urgency": self.urgency, "dedupe": self.dedupe,
            "live": self.live, "at": self.at, "flat": self.flat,
        }

    def dedupe_material(self) -> list:
        if self.dedupe:
            return ["key", self.dedupe]
        return [self.cls, self.source, self.about, self.title, list(self.lines)]


def normalize_lines(value) -> list[str]:
    """Normalize a newline-delimited string or sequence into envelope lines.

    Empty lines create paragraphs, leading and trailing empty lines are dropped,
    and each item becomes one physical line no longer than ``MAX_LINE``.
    """
    if value is None:
        raw = []
    elif isinstance(value, str):
        raw = value.splitlines()
    elif isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raise EnvelopeError("lines: expected a string or list of strings")
    result = []
    for item in raw:
        line = _one_line(item)
        result.append(_clip(line, MAX_LINE) if line else "")
    while result and not result[-1]:
        result.pop()
    while result and not result[0]:
        result.pop(0)
    if len(result) > MAX_LINES:
        raise EnvelopeError(f"lines: more than {MAX_LINES} lines")
    return result


def validate(env: Envelope, classes: dict[str, dict]) -> Envelope:
    """Validate and normalize an envelope using bounds and the class registry."""
    if env.flat:
        if not env.title.strip():
            raise EnvelopeError("empty text cannot be delivered")
        env.source = env.source or "script"
        env.cls = env.cls or "ack"
        env.about = _clip(_one_line(env.about), MAX_ABOUT)
        env.lines = []
    else:
        env.source = _one_line(env.source)
        if _FROM.fullmatch(env.source) is None:
            raise EnvelopeError(f"from: {env.source!r} must be one word of at most 32 characters")
        env.about = _clip(_one_line(env.about), MAX_ABOUT)
        env.title = _clip(_one_line(env.title), MAX_TITLE)
        if not env.title:
            raise EnvelopeError("title is required")
        if not env.about and env.cls != "status":
            raise EnvelopeError("about is required as the unique operation name")
        env.lines = normalize_lines(env.lines)
    if _CLASS.fullmatch(env.cls or "") is None or env.cls not in classes:
        raise EnvelopeError(
            f"class {env.cls!r} is not registered (available: {', '.join(sorted(classes))})")
    env.name = _clip(_one_line(env.name), MAX_NAME)
    env.action = _clip(_one_line(env.action), MAX_ACTION)
    if env.action:
        # Legacy hooks can send an instruction both as a line and in the action
        # field. Keep one action at the end of the rendered envelope.
        env.lines = [line for line in env.lines
                     if not line.casefold().startswith((
                         "what to do:",
                         "\u0447\u0442\u043e \u0434\u0435\u043b\u0430\u0442\u044c:",
                     ))]
    if env.cls == "action" and not env.action:
        raise EnvelopeError("class action requires a requested human action")
    env.urgency = env.urgency or "normal"
    if env.urgency not in URGENCIES:
        raise EnvelopeError(f"urgency {env.urgency!r}: expected one of {', '.join(URGENCIES)}")
    env.dedupe = _one_line(env.dedupe)[:200]
    env.at = _one_line(env.at)[:40]
    env.live = bool(env.live)
    return env


def from_legacy(*, kind: str, source: str, subject: str, title: str, lines: str) -> Envelope:
    """Convert legacy structured flags into a version 2 envelope."""
    if kind not in LEGACY_KINDS:
        raise EnvelopeError(f"unknown kind {kind!r}")
    return Envelope(
        source=_one_line(source), about=subject, cls=LEGACY_KIND_CLASS[kind],
        title=title, lines=normalize_lines(lines), live=(kind == "progress"),
    )


def from_flat(text: str) -> Envelope:
    return Envelope(source="script", about="", cls="ack", title=text, flat=True)


def from_json(raw: bytes | str) -> Envelope:
    """Build an envelope from the JSON object passed to ``deliver --json``."""
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(data) > MAX_JSON_BYTES:
        raise EnvelopeError(f"json: more than {MAX_JSON_BYTES} bytes")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise EnvelopeError(f"json: {error}") from None
    if not isinstance(payload, dict):
        raise EnvelopeError("json: expected an object")
    known = {"from", "about", "class", "title", "lines", "name", "action",
             "urgency", "dedupe", "live", "at"}
    unknown = set(payload) - known
    if unknown:
        raise EnvelopeError(f"json: unknown fields {sorted(unknown)}")
    return Envelope(
        source=str(payload.get("from", "")), about=str(payload.get("about", "")),
        cls=str(payload.get("class", "")), title=str(payload.get("title", "")),
        lines=normalize_lines(payload.get("lines")), name=str(payload.get("name", "") or ""),
        action=str(payload.get("action", "") or ""),
        urgency=str(payload.get("urgency", "normal") or "normal"),
        dedupe=str(payload.get("dedupe", "") or ""), live=bool(payload.get("live", False)),
        at=str(payload.get("at", "") or ""),
    )


def from_dict(payload: dict) -> Envelope:
    """Restore an envelope produced by ``to_dict`` from the deferred queue."""
    return Envelope(
        source=str(payload.get("from", "")), about=str(payload.get("about", "")),
        cls=str(payload.get("class", "")), title=str(payload.get("title", "")),
        lines=[str(item) for item in payload.get("lines", [])],
        name=str(payload.get("name", "")), action=str(payload.get("action", "")),
        urgency=str(payload.get("urgency", "normal")), dedupe=str(payload.get("dedupe", "")),
        live=bool(payload.get("live", False)), at=str(payload.get("at", "")),
        flat=bool(payload.get("flat", False)),
    )
