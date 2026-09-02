"""Fail-closed TOML loading that rejects unknown fields.

Silently accepting a misspelled field creates configuration that appears to
apply but has no effect. Such defects are hard to notice, so an unknown field
always aborts loading.
"""
from __future__ import annotations

import tomllib
from pathlib import Path


def load_toml(path: str | Path, *, known: set[str],
              required: frozenset[str] | set[str] = frozenset()) -> dict:
    """Load TOML, validate top-level fields, and return the raw dictionary.

    Raise ``ValueError`` for invalid syntax, unknown fields, or missing required
    fields. The caller owns defaults and type checking; this layer only enforces
    the agreed field set.
    """
    path = Path(path)
    raw = tomllib.loads(path.read_text("utf-8"))
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(f"{path.name}: unknown fields {sorted(unknown)}")
    missing = set(required) - set(raw)
    if missing:
        raise ValueError(f"{path.name}: missing required fields {sorted(missing)}")
    return raw
