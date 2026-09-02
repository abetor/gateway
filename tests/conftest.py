"""Isolate tests from user data and credentials by replacing HOME, cwd, and env.

A test that reaches the real home directory or writes into the checkout can
silently damage local state. Autouse isolation makes hermetic behavior a
structural property rather than a convention.
"""
import re
import sys
from pathlib import Path

import pytest

# Import the package without installation so tests run from any directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Remove only declared credential variables. Broad heuristics over os.environ
# could delete unrelated path-like variables. Explicit names prevent a local
# credential from turning an intended fake request into a live API call.
_ENV_VAR_RE = re.compile(r"([A-Z][A-Z0-9_]*)=")
_DEFAULT_SECRET_NAMES = ["GATEWAY_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]


def _env_sample_vars(sample: Path | None = None) -> list[str]:
    """Read names from an optional env sample, using safe defaults when absent."""
    if sample is None:
        sample = Path(__file__).resolve().parents[1] / ".env.sample"
    if not sample.is_file():
        return list(_DEFAULT_SECRET_NAMES)
    names = []
    for line in sample.read_text("utf-8").splitlines():
        m = _ENV_VAR_RE.match(line.lstrip("# ").strip())
        if m:
            names.append(m.group(1))
    return names


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    # The underscore avoids collisions with tests that create their own ``home``.
    home = tmp_path / "_isolated_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    for name in _env_sample_vars():
        monkeypatch.delenv(name, raising=False)
