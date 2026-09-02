"""Core JSONL audit log and channel registry."""
from __future__ import annotations

import json

import pytest

import tool_gateway.cli  # noqa: F401 - channel registration point
from tool_gateway.core import log as corelog
from tool_gateway.core.channel import CHANNELS, Channel, make_channel, register


def _rows(home, stream):
    path = home / "log" / f"{stream}.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def test_log_append_creates_dir_and_appends_jsonl(tmp_path):
    corelog.append(tmp_path, "inbound", channel="x", text="hello")
    corelog.append(tmp_path, "inbound", channel="x", text="again")
    rows = _rows(tmp_path, "inbound")
    assert len(rows) == 2 and rows[0]["text"] == "hello" and rows[1]["text"] == "again"
    assert rows[0]["ts"]                           # The log assigns the timestamp.


def test_log_unknown_stream_is_loud(tmp_path):
    with pytest.raises(ValueError, match="stream"):
        corelog.append(tmp_path, "audit", x=1)
    assert not (tmp_path / "log").exists()         # A typo must not create an orphan file.


def test_log_bind_carries_home(tmp_path):
    corelog.bind(tmp_path)("delivery", text="ping", sent=True)
    assert _rows(tmp_path, "delivery")[0]["sent"] is True


def test_registry_has_telegram_and_unknown_is_loud():
    assert "telegram" in CHANNELS
    with pytest.raises(ValueError, match="morse"):
        make_channel("morse")


def test_registry_reregister_idempotent_conflict_loud():
    register("telegram", CHANNELS["telegram"])     # Repeated import is not a conflict.

    class Fake(Channel):
        pass

    with pytest.raises(ValueError, match="already registered"):
        register("telegram", Fake)


def test_make_channel_puts_state_in_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    c = make_channel("telegram", state_dir=str(tmp_path))
    assert c.state_path == str(tmp_path / "channel-telegram.json")
