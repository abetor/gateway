"""Plain delivery compatibility and the platform-failure matrix.

The structured contract is tested separately. Every attempt still writes one
row to ``<home>/log/delivery.jsonl``.
"""
from __future__ import annotations

import json

import pytest

from _helpers import fake_rig
from tool_gateway.cli import EXIT_FAIL, EXIT_OK, EXIT_TRANSIENT, main
from tool_gateway.core.config import load_config
from tool_gateway.ext.deliver.deliver import deliver
from tool_gateway.ext.telegram.channel import CUT_NOTE, MSG_CAP


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "gw-home"
    home.mkdir()
    rig = fake_rig(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    (home / "gateway.toml").write_text(f'''
[channels.telegram]
rig = "{rig}"
[projects.alpha]
path = "{proj}"
''', "utf-8")
    return home


@pytest.fixture(autouse=True)
def _no_ambient_creds(monkeypatch):
    """Ambient operator credentials must not enter hermetic tests."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def _rows(home):
    path = home / "log" / "delivery.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def test_deliver_sends_and_logs_text(home, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t0k")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "714")
    sent = {}

    def http(method, payload):
        sent.update(payload)
        return {"ok": True}

    assert deliver(home, load_config(home), "complete: topic X", http=http) == EXIT_OK
    assert sent["chat_id"] == "714" and "topic X" in sent["text"]
    row = _rows(home)[-1]
    assert row["sent"] is True and row["text"] == "complete: topic X"
    assert row["source"] == "deliver" and row["channel"] == "telegram"


def test_deliver_truncates_wire_text_but_logs_full_text(home, monkeypatch):
    """A truncation marker does not replace the full delivery-log text."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t0k")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "714")
    original = "long report " + "x" * (MSG_CAP + 100)
    sent = {}

    def http(method, payload):
        sent.update(payload)
        return {"ok": True}

    assert deliver(home, load_config(home), original, http=http) == EXIT_OK
    assert len(sent["text"]) == MSG_CAP and sent["text"].endswith(CUT_NOTE)
    assert _rows(home)[-1]["text"] == original


def test_deliver_send_failure_goes_to_retry_queue_and_is_logged(home, monkeypatch):
    """A network failure is queued and logged rather than returned to the caller.

    Exit 111 is reserved for a failure that cannot be written to the retry queue.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t0k")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "714")
    rc = deliver(home, load_config(home), "ping",
                 http=lambda m, p: {"ok": False, "description": "network down"})
    assert rc == EXIT_OK
    row = _rows(home)[-1]
    assert row["sent"] is False and row["text"] == "ping"
    assert row["reason"].startswith("retry: ") and "network down" in row["reason"]
    queued = list((home / "state" / "queue").iterdir())
    assert len(queued) == 1 and row["queued"] == queued[0].name


def test_deliver_send_failure_without_queue_dir_is_transient(home, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t0k")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "714")
    (home / "state").mkdir()
    (home / "state" / "queue").write_text("not a directory", "utf-8")
    rc = deliver(home, load_config(home), "ping",
                 http=lambda m, p: {"ok": False, "description": "network down"})
    assert rc == EXIT_TRANSIENT
    assert "queue" in _rows(home)[-1]["reason"]


@pytest.mark.parametrize("code", [401, 403, 404])
def test_deliver_permanent_platform_refusal_is_fail_not_retry(home, monkeypatch, code):
    """Expired credentials and invalid recipients produce permanent codes.

    Returning 111 here would tell a scheduler to retry a condition that cannot
    repair itself.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t0k")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "714")
    cfg = load_config(home)
    dead = {"ok": False, "error_code": code, "description": f"fatal {code}"}
    assert deliver(home, cfg, "ping", http=lambda m, p: dead) == EXIT_FAIL
    assert f"fatal {code}" in _rows(home)[-1]["reason"]


@pytest.mark.parametrize("response", [
    {"ok": False, "error_code": 400, "description": "temporary Bad Request"},
    {"ok": False, "error_code": 408, "description": "Request Timeout"},
    {"ok": False, "error_code": 429, "description": "Too Many Requests",
     "parameters": {"retry_after": 23}},
    {"ok": False, "error_code": 500, "description": "Internal Server Error"},
    {"ok": False, "error_code": 418, "description": "unknown code"},
    {"ok": False, "description": "URLError: network down"},
])
def test_deliver_transient_platform_failures_are_queued_for_retry(home, monkeypatch, response):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t0k")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "714")
    cfg = load_config(home)
    assert deliver(home, cfg, "ping", http=lambda m, p: response) == EXIT_OK
    assert _rows(home)[-1]["reason"].startswith("retry: ")
    assert len(list((home / "state" / "queue").iterdir())) == 1
    if response.get("error_code") == 429:
        assert "retry_after=23s" in _rows(home)[-1]["reason"]


def test_deliver_missing_creds_logged_as_suppressed(home):
    assert deliver(home, load_config(home), "ping") == EXIT_FAIL
    row = _rows(home)[-1]
    assert row["sent"] is False and "TELEGRAM_BOT_TOKEN" in row["reason"]


def test_deliver_requires_telegram_channel(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    (home / "gateway.toml").write_text("[channels.webhook]\n", "utf-8")
    rc = deliver(home, load_config(home), "ping")
    assert rc == EXIT_FAIL
    assert "[channels.telegram]" in _rows(home)[-1]["reason"]


def test_deliver_refuses_empty_and_unknown_channel(home):
    cfg = load_config(home)
    assert deliver(home, cfg, "   ") == EXIT_FAIL
    assert deliver(home, cfg, "ping", channel_name="morse") == EXIT_FAIL
    reasons = [r["reason"] for r in _rows(home)]
    assert any("empty" in r for r in reasons) and any("morse" in r for r in reasons)


def test_cli_deliver_end_to_end_without_creds(home):
    """The CLI fails honestly and logs text when credentials are absent."""
    assert main(["--home", str(home), "deliver", "hello"]) == EXIT_FAIL
    assert _rows(home)[-1]["text"] == "hello"


def test_cli_deliver_logs_refusal_when_config_missing_or_broken(tmp_path, capsys):
    """A refusal before configuration loading must still preserve delivery text."""
    h = tmp_path / "gw-home"
    h.mkdir()
    assert main(["--home", str(h), "deliver", "config missing, but text matters"]) == EXIT_FAIL
    row = _rows(h)[-1]
    assert row["text"] == "config missing, but text matters"
    assert row["sent"] is False and "gateway.toml" in row["reason"]
    assert row["source"] == "deliver"

    (h / "gateway.toml").write_text("[channels.telegram]\nrig = 42\n", "utf-8")
    assert main(["--home", str(h), "deliver", "config is invalid"]) == EXIT_FAIL
    row = _rows(h)[-1]
    assert row["text"] == "config is invalid" and row["sent"] is False
    assert "gateway.toml" in row["reason"]


def test_cli_structured_refusal_before_config_keeps_all_input(tmp_path):
    home = tmp_path / "gw-home"
    home.mkdir()
    assert main([
        "--home", str(home), "deliver",
        "--kind", "fail",
        "--source", "search",
        "--subject", "rates",
        "--title", "FAILED",
        "--lines", "reason\nLog: /tmp/run.log",
    ]) == EXIT_FAIL
    row = _rows(home)[-1]
    assert row["kind"] == "fail"
    assert row["notice_source"] == "search"
    assert row["subject"] == "rates"
    assert row["title"] == "FAILED"
    assert row["lines"] == ["reason", "Log: /tmp/run.log"]
    assert row["sent"] is False


def test_cli_deliver_without_home_says_where_text_went(tmp_path, capsys):
    """A missing home is not created from a typo; stderr explains the boundary."""
    ghost = tmp_path / "no-such-home"
    assert main(["--home", str(ghost), "deliver", "into the void"]) == EXIT_FAIL
    assert not ghost.exists()
    assert "cannot be recorded" in capsys.readouterr().err
