"""Filesystem primitives for atomic writes and self-ignoring state directories."""
from __future__ import annotations

import os
from pathlib import Path

# A local ignore file protects runtime state in any repository, independently
# of its root ``.gitignore``, so ``git add .`` cannot stage the directory.
GITIGNORE_BODY = "# Tool runtime state is not part of the repository.\n*\n"


def atomic_write(path: str | Path, data: str | bytes, encoding: str = "utf-8") -> None:
    """Write through a sibling temporary file and atomically replace the target.

    Readers never observe a partial file. A crashed process can leave at most a
    temporary fragment, not a corrupt original.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data, encoding=encoding)
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


def ensure_state_dir(path: str | Path) -> Path:
    """Create a state directory with a self-ignoring ``.gitignore``.

    The operation is idempotent and never overwrites an existing ignore file.
    Exclusive creation avoids clobbering during a concurrent race.
    """
    d = Path(path)
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        try:
            with open(gi, "x", encoding="utf-8") as f:
                f.write(GITIGNORE_BODY)
        except FileExistsError:
            pass  # Another process created it between ``exists`` and ``open``.
    return d
