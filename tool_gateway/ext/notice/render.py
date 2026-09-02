"""Render an envelope as Telegram HTML using its class template.

The shared layout is address, class and title, name, facts, then action.
Defaults live here and ``<home>/templates/<class>.txt`` can override them. A
template line is omitted when all of its placeholders are empty. ``{lines}``
expands to envelope lines, where an empty item creates a paragraph break.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from .envelope import Envelope

MAX_TEXT = 3500
_PLACEHOLDER = re.compile(r"\{(head|from|about|name|class|title|lines|action|time)\}")

DEFAULT_TEMPLATES: dict[str, str] = {
    "event": "<b>{head}</b>\n{name}\n{title}\n{lines}",
    "content": "<b>{head}</b>\n{name}\n{title}\n{lines}",
    "alert": "<b>{head}</b>\n{name}\n<b>{class}:</b> {title}\n{lines}\n\n-> {action}",
    "action": "<b>{head}</b>\n{name}\n<b>{class}:</b> {title}\n{lines}\n\n-> {action}",
    "ack": "{head} · {title}\n{lines}",
    "status": "<b>{head} {title}</b>\n{lines}",
}
FALLBACK_TEMPLATE = DEFAULT_TEMPLATES["event"]


def _esc(value: str) -> str:
    return html.escape(value, quote=False)


def template_for(cls: str, home: str | Path | None) -> str:
    if home is not None:
        path = Path(home) / "templates" / f"{cls}.txt"
        try:
            text = path.read_text("utf-8")
        except OSError:
            text = ""
        if text.strip():
            return text.rstrip("\n")
    return DEFAULT_TEMPLATES.get(cls, FALLBACK_TEMPLATE)


def head(env: Envelope) -> str:
    return f"[{env.source}] {env.about}" if env.about else f"[{env.source}]"


def render(env: Envelope, *, home: str | Path | None = None, time_text: str = "") -> str:
    """Return a flat envelope verbatim; its sender uses an empty parse mode."""
    if env.flat:
        return env.title
    values = {
        "head": _esc(head(env)),
        "from": _esc(env.source),
        "about": _esc(env.about),
        "name": _esc(env.name),
        "class": _esc(env.cls),
        "title": _esc(env.title),
        "lines": "\n".join(_esc(line) for line in env.lines),
        "action": _esc(env.action),
        "time": _esc(time_text),
    }
    out: list[str] = []
    for raw in template_for(env.cls, home).split("\n"):
        keys = _PLACEHOLDER.findall(raw)
        if keys and all(not values[key] for key in keys):
            continue
        out.append(_PLACEHOLDER.sub(lambda match: values[match.group(1)], raw))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    if len(text) > MAX_TEXT:
        raise ValueError(f"message exceeds {MAX_TEXT} characters")
    return text
