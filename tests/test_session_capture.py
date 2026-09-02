"""Generated journals must keep ``git diff --check`` free of whitespace errors."""
from __future__ import annotations

import subprocess

from tool_gateway.shared.session_capture import capture


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_capture_separates_blocks_without_blank_line_at_eof(tmp_path):
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\n", "utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "--quiet", "-m", "first")

    journal = capture(tmp_path)
    assert journal is not None
    assert journal.read_text("utf-8").endswith("\n")
    assert not journal.read_text("utf-8").endswith("\n\n")

    tracked.write_text("two\n", "utf-8")
    capture(tmp_path)
    text = journal.read_text("utf-8")
    assert "\n\n## " in text
    assert not text.endswith("\n\n")
