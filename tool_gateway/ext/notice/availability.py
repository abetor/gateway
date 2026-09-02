"""Availability grid that determines when to send or defer a message.

Each row has the form ``mon 00:00-09:50 sleep``. Weekdays may use English
``mon`` through ``sun`` or the legacy two-letter Russian aliases; ``24:00``
means the end of the day. Time not covered by the grid is ``free``. A state
does not defer anything by itself: configuration separately lists the states
that should be deferred.
"""
from __future__ import annotations

import datetime
import re

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_ALIAS = {
    "\u043f\u043d": "mon",
    "\u0432\u0442": "tue",
    "\u0441\u0440": "wed",
    "\u0447\u0442": "thu",
    "\u043f\u0442": "fri",
    "\u0441\u0431": "sat",
    "\u0432\u0441": "sun",
}
_SLOT = re.compile(
    r"^(\S+)\s+(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})\s+([a-z_][a-z0-9_]*)\s*$"
)
_END_OF_DAY = 24 * 60


def _minutes(hours: str, minutes: str, label: str) -> int:
    h, m = int(hours), int(minutes)
    if h == 24 and m == 0:
        return _END_OF_DAY
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"availability: invalid time in {label!r}")
    return h * 60 + m


def parse_grid(rows: list[str]) -> dict[str, list[tuple[int, int, str]]]:
    """Parse rows into ``{day: [(start_minute, end_minute, state)]}``.

    Overlapping windows are rejected.
    """
    grid: dict[str, list[tuple[int, int, str]]] = {day: [] for day in DAYS}
    for raw in rows:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _SLOT.match(line)
        if match is None:
            raise ValueError(f"availability: cannot parse row {line!r}")
        day_raw, h1, m1, h2, m2, state = match.groups()
        day = _DAY_ALIAS.get(day_raw.casefold(), day_raw.casefold())
        if day not in grid:
            raise ValueError(f"availability: unknown day {day_raw!r} in {line!r}")
        start, end = _minutes(h1, m1, line), _minutes(h2, m2, line)
        if start >= end:
            raise ValueError(f"availability: empty window {line!r}")
        for other_start, other_end, _ in grid[day]:
            if start < other_end and other_start < end:
                raise ValueError(f"availability: overlapping windows in {line!r}")
        grid[day].append((start, end, state))
    for slots in grid.values():
        slots.sort()
    return grid


def state_at(grid: dict[str, list[tuple[int, int, str]]],
             when: datetime.datetime) -> str:
    day = DAYS[when.weekday()]
    minute = when.hour * 60 + when.minute
    for start, end, state in grid.get(day, ()):
        if start <= minute < end:
            return state
    return "free"
