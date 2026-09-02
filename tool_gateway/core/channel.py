"""Channel socket: the ``Channel``/``Inbound`` contract and name registry.

The core knows only the contract. Concrete channels such as Telegram live in
``ext/<channel>/`` and call ``register()`` when their modules are imported. The
core does not import ``ext``; ``cli.py`` is the registration point.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Inbound:
    """Normalized inbound event.

    ``text`` is the user's message. ``reply_to_text`` is the text of the message
    being replied to and is used to correlate notification addresses. ``sender``
    is retained for auditing; the channel applies its allow-list before emitting
    the event. ``raw`` is the original channel event.
    """
    text: str
    reply_to_text: str = ""
    sender: str = ""
    raw: object = None


class Channel:
    """Abstract channel contract.

    A concrete class must make ``poll()`` return new ``Inbound`` objects after
    sender allow-listing, advance its own offset, and put platform failures in
    ``last_error`` so the loop can log them. ``send()`` delivers text and
    returns a boolean. ``edit()`` updates a previously delivered message when
    supported. The subclass hides every other platform detail from the core.
    """
    name = "abstract"
    last_error = ""
    # Whether the latest failure was permanent, such as bad credentials or an
    # invalid recipient, rather than a temporary network failure. The channel
    # understands platform codes; the caller maps this flag to exit 1 or 111.
    # Channels that cannot distinguish the cases leave it false.
    last_permanent = False

    def poll(self) -> List[Inbound]:
        raise NotImplementedError

    def send(self, text: str, *, parse_mode: str = "") -> bool:
        raise NotImplementedError

    def edit(self, message_id: int, text: str, *, parse_mode: str = "") -> bool:
        raise NotImplementedError

    def check(self) -> str:
        """Perform a live doctor check; return an empty string on success."""
        return ""


# Registry mapping channel names to classes, populated by ``ext`` imports.
CHANNELS: dict = {}


def register(name: str, cls: type) -> None:
    """Register a channel class, allowing idempotent repeated imports.

    Registering a different class under an occupied name is a hard conflict.
    """
    prev = CHANNELS.get(name)
    if prev is not None and prev is not cls:
        raise ValueError(f"channel {name!r} is already registered by {prev.__name__}")
    CHANNELS[name] = cls


def make_channel(name: str, *, state_dir: str = "", env: Optional[dict] = None,
                 http=None, **kw) -> Channel:
    """Build a named channel using the gateway ``<home>/state`` directory.

    Each channel keeps one offset or cursor file there, not one per project.
    """
    if name not in CHANNELS:
        raise ValueError(f"unknown channel {name!r} (available: {tuple(sorted(CHANNELS))})")
    state = os.path.join(state_dir, f"channel-{name}.json") if state_dir else ""
    return CHANNELS[name](env=env, state_path=state, http=http, **kw)
