"""Telegram chat allow-list, offset, size cap, and visible platform failures.

Credential environment names are configurable and HTTP is injected for
hermetic tests.
"""
from __future__ import annotations

import pytest

from tool_gateway.ext.telegram import channel as tgc


def _tg(tmp_path, http, chat="714"):
    return tgc.TelegramChannel(env={"TELEGRAM_BOT_TOKEN": "t0k", "TELEGRAM_CHAT_ID": chat},
                               state_path=str(tmp_path / "state.json"), http=http)


def test_requires_creds_fail_closed():
    with pytest.raises(ValueError):
        tgc.TelegramChannel(env={"TELEGRAM_BOT_TOKEN": "t"}, state_path="")
    with pytest.raises(ValueError):
        tgc.TelegramChannel(env={"TELEGRAM_CHAT_ID": "1"}, state_path="")


def test_custom_env_names_from_config():
    c = tgc.TelegramChannel(env={"MY_BOT": "t", "MY_CHAT": "5"},
                            token_env="MY_BOT", chat_id_env="MY_CHAT",
                            state_path="", http=lambda m, p: {"ok": True})
    assert c.chat_id == "5"
    with pytest.raises(ValueError, match="MY_BOT"):    # Refusal names the configured variables.
        tgc.TelegramChannel(env={}, token_env="MY_BOT", chat_id_env="MY_CHAT", state_path="")


def test_poll_filters_foreign_chat_and_moves_offset(tmp_path):
    updates = {"ok": True, "result": [
        {"update_id": 10, "message": {"chat": {"id": 714}, "text": "#3 approve",
                                      "reply_to_message": {"text": "rig #3 · proj · lead"}}},
        {"update_id": 11, "message": {"chat": {"id": 999}, "text": "intruder"}},
        {"update_id": 12, "message": {"chat": {"id": 714}}},                     # Ignore non-text.
    ]}
    calls = []

    def http(method, payload):
        calls.append((method, payload))
        return updates if method == "getUpdates" else {"ok": True}

    c = _tg(tmp_path, http)
    msgs = c.poll()
    assert [m.text for m in msgs] == ["#3 approve"] and c.offset == 12
    c2 = _tg(tmp_path, http)                       # Persisted offset prevents replay after restart.
    assert c2.offset == 12
    c2.poll()
    assert calls[-1][1]["offset"] == 13


def test_send_caps_length_and_says_it_cut(tmp_path):
    """Truncation of a long stdin report must be visible in the message itself."""
    sent = {}

    def http(method, payload):
        sent.update(payload)
        return {"ok": True}

    assert _tg(tmp_path, http).send("x" * 10000) is True
    assert len(sent["text"]) <= tgc.MSG_CAP and sent["chat_id"] == "714"
    assert sent["text"].endswith(tgc.CUT_NOTE)
    _tg(tmp_path, http).send("short")              # Content within the cap stays unchanged.
    assert sent["text"] == "short"


@pytest.mark.parametrize("code", [401, 403, 404])
def test_send_fatal_error_matrix(tmp_path, code):
    """Only established client errors are permanent and require operator action."""
    c = _tg(tmp_path, lambda m, p: {
        "ok": False, "error_code": code, "description": f"fatal {code}",
    })
    assert c.send("ping") is False
    assert c.last_permanent is True
    assert f"fatal {code}" in c.last_error


@pytest.mark.parametrize("response", [
    {"ok": False, "error_code": 400, "description": "temporary Bad Request"},
    {"ok": False, "error_code": 408, "description": "Request Timeout"},
    {"ok": False, "error_code": 429, "description": "Too Many Requests",
     "parameters": {"retry_after": 17}},
    {"ok": False, "error_code": 500, "description": "Internal Server Error"},
    {"ok": False, "error_code": 418, "description": "unknown code"},
    {"ok": False, "description": "URLError: network down"},
])
def test_send_transient_error_matrix(tmp_path, response):
    """Transport failures and unknown codes remain retryable to avoid lost notices."""
    c = _tg(tmp_path, lambda m, p: response)
    assert c.send("ping") is False
    assert c.last_permanent is False
    if response.get("error_code") == 429:
        assert "retry_after=17s" in c.last_error


def test_poll_sets_last_error_and_check_pings(tmp_path):
    def bad_http(method, payload):
        return {"ok": False, "description": "Unauthorized"}

    c = _tg(tmp_path, bad_http)
    assert c.poll() == [] and "Unauthorized" in c.last_error
    assert "Unauthorized" in c.check()
    ok = _tg(tmp_path, lambda m, p: {"ok": True, "result": []})
    assert ok.check() == ""
