"""Legacy structured flags through the version 2 envelope path.

Tests cover formatting, collision-safe live state, edit pacing, live-message
closure, and per-class deduplication.
"""
from __future__ import annotations

import io
import json

import pytest

from tool_gateway.cli import EXIT_FAIL, EXIT_OK, main
from tool_gateway.core.config import load_config
from tool_gateway.ext.deliver.deliver import (
    CLOSE_SUFFIX,
    _live_path,
    deliver,
)
from tool_gateway.ext.notice.envelope import Envelope, EnvelopeError, validate, DEFAULT_CLASSES
from tool_gateway.ext.notice.render import MAX_TEXT, render


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "gw-home"
    home.mkdir()
    (home / "gateway.toml").write_text("[channels.telegram]\n", "utf-8")
    return home


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "714")


class FakeHTTP:
    def __init__(self, *, edit_error: str = ""):
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 100
        self.edit_error = edit_error

    def __call__(self, method, payload):
        self.calls.append((method, dict(payload)))
        if method == "editMessageText" and self.edit_error:
            return {"ok": False, "error_code": 400, "description": self.edit_error}
        if method == "sendMessage":
            self.next_id += 1
            return {"ok": True, "result": {"message_id": self.next_id}}
        return {"ok": True, "result": {"message_id": payload["message_id"]}}


def rows(home):
    path = home / "log" / "delivery.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def live_path(home):
    return _live_path(home, "search", "rates")


def notice(home, http, at, *, kind="progress", title="cycle 1/3", lines="line",
           pause=None):
    return deliver(
        home,
        load_config(home),
        "",
        kind=kind,
        source="search",
        subject="rates",
        title=title,
        lines=lines,
        structured=True,
        http=http,
        now=lambda: at,
        pause=pause,
    )


def test_legacy_done_renders_as_event_envelope_in_html(home):
    http = FakeHTTP()
    assert notice(home, http, 1000, kind="done", title="complete",
                  lines="27 claims\nDirectory: /tmp/result") == EXIT_OK
    method, payload = http.calls[0]
    assert method == "sendMessage"
    assert payload["parse_mode"] == "HTML"
    assert payload["text"] == "<b>[search] rates</b>\ncomplete\n27 claims\nDirectory: /tmp/result"
    row = rows(home)[-1]
    assert row["kind"] == "event" and row["class"] == "event"
    assert row["from"] == "search" and row["about"] == "rates" and row["subject"] == "rates"


def test_legacy_fail_renders_as_alert_with_class_marker(home):
    http = FakeHTTP()
    assert notice(home, http, 1000, kind="fail", title="failed",
                  lines="reason\nWhat to do: resume") == EXIT_OK
    assert http.calls[0][1]["text"] == (
        "<b>[search] rates</b>\n<b>alert:</b> failed\nreason\nWhat to do: resume"
    )
    assert rows(home)[-1]["class"] == "alert"


def test_all_user_fields_are_html_escaped(home):
    http = FakeHTTP()
    assert deliver(home, load_config(home), "", kind="done", source="search",
                   subject="subject >", title="complete <&>", lines="a < b & c > d",
                   structured=True, http=http, now=lambda: 1000) == EXIT_OK
    sent = http.calls[0][1]["text"]
    assert sent == "<b>[search] subject &gt;</b>\ncomplete &lt;&amp;&gt;\na &lt; b &amp; c &gt; d"


def test_from_with_spaces_or_brackets_is_refused(home):
    rc = deliver(home, load_config(home), "", kind="done", source="s<rch&",
                 subject="x", title="complete", structured=True, http=FakeHTTP(),
                 now=lambda: 1000)
    assert rc == EXIT_FAIL
    assert "from" in rows(home)[-1]["reason"]


def test_more_than_forty_lines_is_refused():
    env = Envelope(source="search", about="topic", cls="event", title="stage",
                   lines=[str(i) for i in range(41)])
    with pytest.raises(EnvelopeError):
        validate(env, DEFAULT_CLASSES)


def test_each_line_is_clipped_to_200_with_ellipsis():
    env = validate(Envelope(source="search", about="topic", cls="event", title="stage",
                            lines=["x" * 201]), DEFAULT_CLASSES)
    line = render(env).splitlines()[2]
    assert len(line) == 200 and line.endswith("…")


def test_escaping_cannot_push_total_over_limit():
    env = validate(Envelope(source="search", about="&" * 100, cls="event", title="&" * 1000,
                            lines=["&" * 1000] * 40), DEFAULT_CLASSES)
    with pytest.raises(ValueError):
        render(env)
    assert MAX_TEXT == 3500


def test_flat_mode_remains_unformatted_without_parse_mode(home):
    http = FakeHTTP()
    flat = "verbatim <b>& without HTML</b>"
    assert deliver(home, load_config(home), flat, http=http, now=lambda: 1000) == EXIT_OK
    payload = http.calls[0][1]
    assert payload["text"] == flat
    assert "parse_mode" not in payload
    assert rows(home)[-1]["kind"] == "ack"


def test_first_progress_sends_new_and_writes_exact_live_pointer(home):
    http = FakeHTTP()
    assert notice(home, http, 1000) == EXIT_OK
    state = json.loads(live_path(home).read_text("utf-8"))
    assert set(state) == {"message_id", "sent_at", "text_hash"}
    assert state["message_id"] == 101 and state["sent_at"] == 1000
    assert len(state["text_hash"]) == 64
    assert rows(home)[-1]["mode"] == "new" and rows(home)[-1]["live"] is True


def test_live_pointer_hash_distinguishes_equal_slugs(home):
    slash = _live_path(home, "a/b", "topic")
    dash = _live_path(home, "a-b", "topic")
    assert slash != dash
    assert slash.name.startswith("a-b__topic-")
    assert dash.name.startswith("a-b__topic-")
    assert len(slash.stem.rsplit("-", 1)[1]) == 10


def test_fresh_progress_edits_same_message_after_one_minute(home):
    http = FakeHTTP()
    assert notice(home, http, 1000, title="cycle 1/3") == EXIT_OK
    assert notice(home, http, 1060, title="cycle 2/3") == EXIT_OK
    assert [method for method, _ in http.calls] == ["sendMessage", "editMessageText"]
    assert http.calls[-1][1]["message_id"] == 101
    state = json.loads(live_path(home).read_text("utf-8"))
    assert state["message_id"] == 101 and state["sent_at"] == 1060
    assert rows(home)[-1]["mode"] == "edit"


def test_progress_older_than_24_hours_is_replaced_by_new_message(home):
    http = FakeHTTP()
    assert notice(home, http, 1000, title="cycle 1/3") == EXIT_OK
    assert notice(home, http, 1000 + 24 * 60 * 60, title="cycle 2/3") == EXIT_OK
    assert [method for method, _ in http.calls] == ["sendMessage", "sendMessage"]
    assert json.loads(live_path(home).read_text("utf-8"))["message_id"] == 102


@pytest.mark.parametrize("kind,title,cls", [
    ("done", "complete", "event"), ("fail", "failed", "alert"),
    ("wait", "waiting until 12:45", "event"), ("stall", "stall 90m", "alert"),
])
def test_non_live_event_or_alert_closes_and_removes_live_pointer(home, kind, title, cls):
    http = FakeHTTP()
    assert notice(home, http, 1000, title="cycle 1/3", lines="making progress") == EXIT_OK
    old_text = http.calls[0][1]["text"]
    assert notice(home, http, 1061, kind=kind, title=title,
                  lines="3/3 cycles") == EXIT_OK
    assert [method for method, _ in http.calls] == [
        "sendMessage", "sendMessage", "editMessageText",
    ]
    assert http.calls[-1][1]["message_id"] == 101
    assert http.calls[-1][1]["text"] == old_text + CLOSE_SUFFIX
    assert not live_path(home).exists()
    assert rows(home)[-1]["kind"] == cls and rows(home)[-1]["sent"] is True


def test_ack_keeps_live_pointer(home):
    http = FakeHTTP()
    assert notice(home, http, 1000, title="cycle 1/3") == EXIT_OK
    before = live_path(home).read_bytes()
    assert notice(home, http, 1061, kind="info", title="state") == EXIT_OK
    assert live_path(home).read_bytes() == before
    assert [method for method, _ in http.calls] == ["sendMessage", "sendMessage"]
    assert http.calls[-1][1]["text"] == "[search] rates · state\nline"


def test_identical_event_within_30_minutes_is_deduplicated(home, capsys):
    http = FakeHTTP()
    assert notice(home, http, 1000, kind="done", title="complete") == EXIT_OK
    assert notice(home, http, 2799, kind="done", title="complete") == EXIT_OK
    assert [method for method, _ in http.calls] == ["sendMessage"]
    assert rows(home)[-1]["sent"] is False and rows(home)[-1]["reason"] == "dup"
    assert "deliver: delivered (dup)" in capsys.readouterr().out


def test_same_event_at_30_minute_boundary_is_sent_again(home):
    http = FakeHTTP()
    assert notice(home, http, 1000, kind="done", title="complete") == EXIT_OK
    assert notice(home, http, 2800, kind="done", title="complete") == EXIT_OK
    assert [method for method, _ in http.calls] == ["sendMessage", "sendMessage"]


def test_alert_is_deduplicated_for_two_hours(home):
    http = FakeHTTP()
    assert notice(home, http, 1000, kind="stall", title="stall 90m") == EXIT_OK
    assert notice(home, http, 8199, kind="stall", title="stall 90m") == EXIT_OK
    assert rows(home)[-1]["sent"] is False and rows(home)[-1]["reason"] == "dup"
    assert notice(home, http, 8200, kind="stall", title="stall 90m") == EXIT_OK
    assert [method for method, _ in http.calls] == ["sendMessage", "sendMessage"]


def test_dedup_state_drops_entries_older_than_a_day(home):
    dedup = home / "state" / "dedup.json"
    dedup.parent.mkdir()
    dedup.write_text(json.dumps({"0" * 64: 1}), "utf-8")
    assert notice(home, FakeHTTP(), 24 * 3600 + 2, kind="done", title="complete") == EXIT_OK
    state = json.loads(dedup.read_text("utf-8"))
    assert "0" * 64 not in state
    assert len(state) == 1


def test_progress_waits_only_until_three_second_edit_interval(home):
    http = FakeHTTP()
    pauses = []
    assert notice(home, http, 1000, title="cycle 1/3") == EXIT_OK
    assert notice(home, http, 1001, title="cycle 2/3", pause=pauses.append) == EXIT_OK
    assert pauses == [2.0]
    assert [method for method, _ in http.calls] == ["sendMessage", "editMessageText"]
    assert json.loads(live_path(home).read_text("utf-8"))["sent_at"] == 1003


def test_same_progress_is_local_noop_without_edit(home):
    http = FakeHTTP()
    assert notice(home, http, 1000) == EXIT_OK
    assert notice(home, http, 1001) == EXIT_OK
    assert [method for method, _ in http.calls] == ["sendMessage"]
    assert rows(home)[-1]["sent"] is False and rows(home)[-1]["reason"] in {"same", "dup"}


def test_stdin_uses_first_line_as_title_and_rest_as_lines(home, monkeypatch):
    calls = []

    def fake_http(_self, method, payload):
        calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 77}}

    monkeypatch.setattr(
        "tool_gateway.ext.telegram.channel.TelegramChannel._urllib_http", fake_http,
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("complete\n27 claims\nDirectory: /tmp/x\n"))
    assert main(["--home", str(home), "deliver", "--stdin", "--kind", "done",
                 "--source", "search", "--subject", "rates"]) == EXIT_OK
    assert calls[0][1]["text"] == "<b>[search] rates</b>\ncomplete\n27 claims\nDirectory: /tmp/x"


def test_flat_stdin_keeps_entire_text_unformatted(home, monkeypatch):
    calls = []

    def fake_http(_self, method, payload):
        calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 77}}

    monkeypatch.setattr(
        "tool_gateway.ext.telegram.channel.TelegramChannel._urllib_http", fake_http,
    )
    raw = "verbatim <b>&</b>\nsecond line\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert main(["--home", str(home), "deliver", "--stdin"]) == EXIT_OK
    assert calls[0][1]["text"] == raw
    assert "parse_mode" not in calls[0][1]


def test_edit_error_falls_back_to_new_message_and_replaces_pointer(home):
    http = FakeHTTP()
    assert notice(home, http, 1000, title="cycle 1/3") == EXIT_OK
    http.edit_error = "message is gone"
    assert notice(home, http, 1060, title="cycle 2/3") == EXIT_OK
    assert [method for method, _ in http.calls] == [
        "sendMessage", "editMessageText", "sendMessage",
    ]
    assert json.loads(live_path(home).read_text("utf-8"))["message_id"] == 102
    assert rows(home)[-1]["mode"] == "new"
    assert "message is gone" in rows(home)[-1]["edit_error"]


def test_telegram_not_modified_is_success_without_new_message(home):
    http = FakeHTTP()
    assert notice(home, http, 1000, title="cycle 1/3") == EXIT_OK
    http.edit_error = "Bad Request: message is not modified"
    assert notice(home, http, 1060, title="cycle 2/3") == EXIT_OK
    assert [method for method, _ in http.calls] == ["sendMessage", "editMessageText"]
    assert json.loads(live_path(home).read_text("utf-8"))["message_id"] == 101
    assert rows(home)[-1]["sent"] is True and rows(home)[-1]["mode"] == "edit"


def test_close_edit_failure_is_logged_but_terminal_stays_successful(home):
    http = FakeHTTP()
    assert notice(home, http, 1000, title="cycle 1/3") == EXIT_OK
    http.edit_error = "message is gone"
    assert notice(home, http, 1060, kind="done", title="complete") == EXIT_OK
    assert not live_path(home).exists()
    close_row, terminal_row = rows(home)[-2:]
    assert close_row["sent"] is False and close_row["reason"] == "close_edit_failed"
    assert "message is gone" in close_row["edit_error"]
    assert terminal_row["sent"] is True and terminal_row["kind"] == "event"


def test_structured_notice_refuses_missing_source(home):
    rc = deliver(home, load_config(home), "", kind="done", subject="rates",
                 title="complete", structured=True, http=FakeHTTP(), now=lambda: 1000)
    assert rc == EXIT_FAIL
    assert rows(home)[-1]["sent"] is False
    assert "source" in rows(home)[-1]["reason"]
