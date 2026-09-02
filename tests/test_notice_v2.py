"""Version 2 envelope classes, templates, availability, queue, and snapshot."""
from __future__ import annotations

import datetime
import io
import json

import pytest

from tool_gateway.cli import EXIT_FAIL, EXIT_OK, main
from tool_gateway.core.config import load_config
from tool_gateway.ext.deliver.deliver import deliver_envelope, drain
from tool_gateway.ext.notice import queue as notice_queue
from tool_gateway.ext.notice.availability import parse_grid, state_at
from tool_gateway.ext.notice.envelope import (
    DEFAULT_CLASSES,
    Envelope,
    EnvelopeError,
    from_json,
    from_legacy,
    validate,
)
from tool_gateway.ext.notice.render import render
from tool_gateway.ext.notice.snapshot import fmt_duration, parse_fragment, snapshot

GRID = [
    "mon 00:00-09:50 sleep", "mon 09:50-18:10 work_home", "mon 18:10-20:10 gym",
    "mon 20:10-24:00 free",
]
# 2026-08-24 is a Monday.
MONDAY = datetime.datetime(2026, 8, 24, 12, 0).astimezone()


def ts(hour: int, minute: int = 0) -> float:
    return datetime.datetime(2026, 8, 24, hour, minute).astimezone().timestamp()


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "gw-home"
    home.mkdir()
    (home / "gateway.toml").write_text("[channels.telegram]\n", "utf-8")
    return home


@pytest.fixture
def home_with_windows(home):
    grid = "\n".join(f'  "{row}",' for row in GRID)
    (home / "gateway.toml").write_text(
        f"[channels.telegram]\n[notify]\navailability = [\n{grid}\n]\n"
        "defer_max_age = \"12h\"\n[notify.classes.alert]\ndedupe_seconds = 60\n"
        "[notify.classes.nudge]\ndedupe_seconds = 0\ndefer = true\n", "utf-8")
    return home


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "714")


class FakeHTTP:
    def __init__(self, *, fail: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 100
        self.fail = fail

    def __call__(self, method, payload):
        self.calls.append((method, dict(payload)))
        if self.fail is not None:
            return self.fail
        if method == "sendMessage":
            self.next_id += 1
            return {"ok": True, "result": {"message_id": self.next_id}}
        return {"ok": True, "result": {"message_id": payload["message_id"]}}


def rows(home):
    return [json.loads(line) for line in (home / "log" / "delivery.jsonl").read_text("utf-8").splitlines()]


def env(**overrides) -> Envelope:
    base = dict(source="coursedump", about="maven-eteaiengbtcmp", cls="event",
                title="extract -> verify", lines=["3/4 verify 0/83", "1h22/~9h · ~20:00"],
                name="End-to-End AI Engineering Bootcamp")
    base.update(overrides)
    return Envelope(**base)


# --- envelope and rendering --------------------------------------------------

def test_event_renders_head_name_title_lines():
    text = render(validate(env(), DEFAULT_CLASSES))
    assert text == (
        "<b>[coursedump] maven-eteaiengbtcmp</b>\nEnd-to-End AI Engineering Bootcamp\n"
        "extract -&gt; verify\n3/4 verify 0/83\n1h22/~9h · ~20:00"
    )


def test_alert_and_action_render_class_marker_and_arrow():
    alert = validate(env(cls="alert", title="fail · extract 53/83", lines=["codex: rejected 3 times"],
                         action="scheduler run course-queue-2"), DEFAULT_CLASSES)
    assert render(alert) == (
        "<b>[coursedump] maven-eteaiengbtcmp</b>\nEnd-to-End AI Engineering Bootcamp\n"
        "<b>alert:</b> fail · extract 53/83\ncodex: rejected 3 times\n\n-> scheduler run course-queue-2"
    )
    action = validate(env(cls="action", title="plan review", name="", lines=[],
                          action="scheduler run supervise-sobes-meta"), DEFAULT_CLASSES)
    assert render(action) == (
        "<b>[coursedump] maven-eteaiengbtcmp</b>\n<b>action:</b> plan review\n\n"
        "-> scheduler run supervise-sobes-meta"
    )


def test_action_class_requires_action():
    with pytest.raises(EnvelopeError):
        validate(env(cls="action", action=""), DEFAULT_CLASSES)


def test_ack_and_status_templates():
    ack = validate(env(cls="ack", title="daemon start", lines=["jobs 14 · running 3"], name=""),
                   DEFAULT_CLASSES)
    assert render(ack) == "[coursedump] maven-eteaiengbtcmp · daemon start\njobs 14 · running 3"
    status = validate(Envelope(source="status", about="", cls="status", title="12:00 · 2 workers",
                               lines=["[coursedump] 2 workers", "w1 x", "", "[researcher] 0 workers"]),
                      DEFAULT_CLASSES)
    assert render(status) == (
        "<b>[status] 12:00 · 2 workers</b>\n[coursedump] 2 workers\nw1 x\n\n[researcher] 0 workers"
    )


def test_about_required_except_status():
    with pytest.raises(EnvelopeError):
        validate(env(about=""), DEFAULT_CLASSES)


def test_unknown_class_is_refused_and_config_can_add_one(home_with_windows):
    with pytest.raises(EnvelopeError):
        validate(env(cls="nudge"), DEFAULT_CLASSES)
    cfg = load_config(home_with_windows)
    assert cfg.notify.classes["nudge"] == {"dedupe_seconds": 0, "defer": True}
    assert cfg.notify.classes["alert"]["dedupe_seconds"] == 60
    assert render(validate(env(cls="nudge"), cfg.notify.classes)).startswith("<b>[coursedump]")


def test_template_override_from_home(home):
    (home / "templates").mkdir()
    (home / "templates" / "event.txt").write_text("{head} | {title}\n{lines}\n", "utf-8")
    text = render(validate(env(lines=["a"]), DEFAULT_CLASSES), home=home)
    assert text == "[coursedump] maven-eteaiengbtcmp | extract -&gt; verify\na"


def test_legacy_mapping_and_json_envelope():
    legacy = from_legacy(kind="progress", source="search", subject="rates", title="cycle 1/3",
                         lines="a\nb")
    assert (legacy.cls, legacy.live, legacy.lines) == ("event", True, ["a", "b"])
    parsed = from_json(json.dumps({
        "from": "rig", "about": "rig #3", "class": "action", "title": "plan ready",
        "lines": ["goal: circle area"], "action": "rig --reply 3 approve", "urgency": "high",
    }).encode("utf-8"))
    assert validate(parsed, DEFAULT_CLASSES).urgency == "high"
    with pytest.raises(EnvelopeError):
        from_json(b'{"from": "x", "bogus": 1}')
    with pytest.raises(EnvelopeError):
        from_json(b"[1, 2]")


# --- availability windows and queue ------------------------------------------

def test_grid_parses_days_and_reports_state():
    grid = parse_grid(GRID + ["tue 01:00-09:50 sleep"])
    assert state_at(grid, MONDAY.replace(hour=3)) == "sleep"
    assert state_at(grid, MONDAY.replace(hour=19)) == "gym"
    assert state_at(grid, MONDAY.replace(hour=23, minute=59)) == "free"
    assert state_at(grid, MONDAY.replace(day=25, hour=5)) == "sleep"
    assert state_at(grid, MONDAY.replace(day=26, hour=5)) == "free"
    with pytest.raises(ValueError):
        parse_grid(["mon 10:00-09:00 sleep"])
    with pytest.raises(ValueError):
        parse_grid(["mon 00:00-10:00 sleep", "mon 09:00-11:00 gym"])
    with pytest.raises(ValueError):
        parse_grid(["evening 00:00-10:00 sleep"])


def test_sleep_defers_normal_and_drain_sends_when_awake(home_with_windows, capsys):
    cfg = load_config(home_with_windows)
    http = FakeHTTP()
    assert deliver_envelope(home_with_windows, cfg, env(), http=http, now=lambda: ts(3)) == EXIT_OK
    assert http.calls == []
    row = rows(home_with_windows)[-1]
    assert row["sent"] is False and row["reason"] == "deferred:sleep"
    assert len(notice_queue.pending(home_with_windows)) == 1
    assert "deferred (sleep)" in capsys.readouterr().out

    assert drain(home_with_windows, cfg, http=http, now=lambda: ts(4)) == EXIT_OK
    assert http.calls == [] and len(notice_queue.pending(home_with_windows)) == 1

    assert drain(home_with_windows, cfg, http=http, now=lambda: ts(10)) == EXIT_OK
    assert [m for m, _ in http.calls] == ["sendMessage"]
    assert notice_queue.pending(home_with_windows) == []
    sent = rows(home_with_windows)[-1]
    assert sent["sent"] is True and sent["queued"] == "deferred:sleep"


def test_high_urgency_and_action_class_bypass_windows(home_with_windows):
    cfg = load_config(home_with_windows)
    http = FakeHTTP()
    assert deliver_envelope(home_with_windows, cfg, env(urgency="high"), http=http,
                            now=lambda: ts(3)) == EXIT_OK
    assert deliver_envelope(home_with_windows, cfg,
                            env(cls="action", title="needs you", action="rig --approve 4"),
                            http=http, now=lambda: ts(3, 5)) == EXIT_OK
    assert [m for m, _ in http.calls] == ["sendMessage", "sendMessage"]


def test_deliver_drains_queue_before_itself_keeping_order(home_with_windows):
    cfg = load_config(home_with_windows)
    http = FakeHTTP()
    assert deliver_envelope(home_with_windows, cfg, env(title="first"), http=http,
                            now=lambda: ts(3)) == EXIT_OK
    assert deliver_envelope(home_with_windows, cfg, env(title="second"), http=http,
                            now=lambda: ts(11)) == EXIT_OK
    texts = [payload["text"] for _, payload in http.calls]
    assert [t.splitlines()[2] for t in texts] == ["first", "second"]
    assert notice_queue.pending(home_with_windows) == []


def test_deferred_older_than_max_age_is_dropped_with_reason(home_with_windows):
    cfg = load_config(home_with_windows)
    http = FakeHTTP()
    assert deliver_envelope(home_with_windows, cfg, env(), http=http, now=lambda: ts(3)) == EXIT_OK
    assert drain(home_with_windows, cfg, http=http, now=lambda: ts(3) + 13 * 3600) == EXIT_OK
    assert http.calls == [] and notice_queue.pending(home_with_windows) == []
    assert rows(home_with_windows)[-1]["reason"].startswith("dropped:age")


def test_retry_queue_resends_after_network_recovers(home):
    cfg = load_config(home)
    http = FakeHTTP(fail={"ok": False, "description": "network down"})
    assert deliver_envelope(home, cfg, env(), http=http, now=lambda: ts(12)) == EXIT_OK
    assert len(notice_queue.pending(home)) == 1
    assert drain(home, cfg, http=http, now=lambda: ts(12, 10)) == EXIT_OK
    assert len(notice_queue.pending(home)) == 1
    meta = json.loads(notice_queue.pending(home)[0].read_text("utf-8"))
    assert meta["attempts"] == 2
    http.fail = None
    assert drain(home, cfg, http=http, now=lambda: ts(12, 20)) == EXIT_OK
    assert notice_queue.pending(home) == []
    assert rows(home)[-1]["sent"] is True and rows(home)[-1]["queued"] == "retry"


def test_permanent_refusal_during_drain_drops_item(home):
    cfg = load_config(home)
    http = FakeHTTP(fail={"ok": False, "description": "network down"})
    assert deliver_envelope(home, cfg, env(), http=http, now=lambda: ts(12)) == EXIT_OK
    http.fail = {"ok": False, "error_code": 401, "description": "Unauthorized"}
    assert drain(home, cfg, http=http, now=lambda: ts(12, 10)) == EXIT_OK
    assert notice_queue.pending(home) == []
    assert "platform refusal" in rows(home)[-1]["reason"]


def test_corrupt_queue_file_is_removed_and_logged(home):
    cfg = load_config(home)
    notice_queue.queue_dir(home).mkdir(parents=True)
    (notice_queue.queue_dir(home) / "1700000000000-deadbeef.json").write_text("{", "utf-8")
    (notice_queue.queue_dir(home) / "junk.txt").write_text("x", "utf-8")
    assert drain(home, cfg, http=FakeHTTP(), now=lambda: ts(12)) == EXIT_OK
    assert [p.name for p in notice_queue.queue_dir(home).iterdir()] == ["junk.txt"]
    assert "corrupt file" in rows(home)[-1]["reason"]


# --- CLI -------------------------------------------------------------------------

def _patch_http(monkeypatch, calls):
    def fake_http(_self, method, payload):
        calls.append((method, payload))
        return {"ok": True, "result": {"message_id": 77}}
    monkeypatch.setattr("tool_gateway.ext.telegram.channel.TelegramChannel._urllib_http", fake_http)


def test_cli_v2_flags(home, monkeypatch):
    calls = []
    _patch_http(monkeypatch, calls)
    rc = main(["--home", str(home), "deliver", "--from", "scheduler", "--about", "course-queue-2",
               "--class", "alert", "--title", "stall 90m", "--line", "90m no movement in log",
               "--line", "last: extract 53/83", "--action",
               "scheduler stop course-queue-2 && scheduler run course-queue-2"])
    assert rc == EXIT_OK
    assert calls[0][1]["text"] == (
        "<b>[scheduler] course-queue-2</b>\n<b>alert:</b> stall 90m\n90m no movement in log\n"
        "last: extract 53/83\n\n-> scheduler stop course-queue-2 &amp;&amp; scheduler run course-queue-2"
    )
    row = rows(home)[-1]
    assert row["class"] == "alert" and row["action"].startswith("scheduler stop")


def test_cli_v2_json_stdin_and_mixing_refused(home, monkeypatch, capsys):
    calls = []
    _patch_http(monkeypatch, calls)
    payload = {"from": "rig", "about": "rig #3 · proj", "class": "event",
               "title": "flow complete", "lines": ["units green 2/3"]}
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8"))))
    assert main(["--home", str(home), "deliver", "--json"]) == EXIT_OK
    assert calls[0][1]["text"] == "<b>[rig] rig #3 · proj</b>\nflow complete\nunits green 2/3"
    with pytest.raises(SystemExit) as stop:
        main(["--home", str(home), "deliver", "--from", "rig", "--source", "search", "--title", "x"])
    assert stop.value.code == EXIT_FAIL
    assert "cannot be mixed" in capsys.readouterr().err


def test_cli_v2_refusal_before_config_logs_envelope(tmp_path, capsys):
    home = tmp_path / "gw-home"
    home.mkdir()
    rc = main(["--home", str(home), "deliver", "--from", "x", "--about", "y",
               "--class", "event", "--title", "t"])
    assert rc == EXIT_FAIL
    row = rows(home)[-1]
    assert row["sent"] is False and row["envelope"]["title"] == "t"


# --- snapshot ------------------------------------------------------------------

COURSES = {"snapshot": {"flow": "coursedump", "workers": [
    {"item": "maven-eteaiengbtcmp", "name": "End-to-End AI Engineering Bootcamp", "pid": 60744,
     "stage": {"i": 1, "n": 3, "name": "extract"}, "done": 52, "total": 83,
     "started_at": "2026-08-24T05:58:50+00:00", "eta_seconds": 1860, "eta_total_seconds": 31000,
     "state": "running", "metrics": ["windows 276"]},
    {"item": "fm-harnessengagorws", "pid": 70001, "stage": {"i": 3, "n": 3, "name": "pages"},
     "done": 3, "total": 23, "started_at": "2026-08-24T06:40:00+00:00", "eta_seconds": 3900},
], "queue": [
    {"item": "bc-aiagentscourse", "state": "waiting"}, {"item": "ab-tappliedaimc", "state": "waiting"},
    {"item": "done-course", "state": "done"},
]}}
SEARCHES = {"snapshot": {"flow": "researcher", "workers": [
    {"item": "sobes-otchety", "name": "Real senior Go engineer interview loop", "pid": 80001,
     "stage": {"i": 1, "n": 3, "name": "cycles"}, "done": 3, "total": 24,
     "started_at": "2026-08-24T05:52:00+00:00", "note": "judge: continue - not enough first-hand",
     "metrics": ["claims 193", "sources 54", "map 10/10"]}], "queue": []}}
JOBS = {"jobs": [
    {"name": "course-queue-2", "state": {"runtime": {"kind": "stall", "pgid": 60742, "pid": 60740,
                                                      "since": "2026-08-24T05:58:50+00:00",
                                                      "stall_minutes": 95}}},
    {"name": "course-queue", "state": {"runtime": {"kind": "running", "pgid": 70000, "pid": 69999,
                                                    "since": "2026-08-24T06:40:00+00:00"}}},
    {"name": "nochnoj-sintez", "state": {"runtime": {"kind": "running", "pgid": 90000, "pid": 89999,
                                                      "since": "2026-08-24T06:42:00+00:00"}}},
    {"name": "sintez-podacha", "state": {"runtime": {"kind": "waiting", "pgid": None, "pid": None,
                                                      "until": "2026-08-24T08:00:00+00:00"}}},
]}
PGIDS = {60744: 60742, 70001: 70000, 80001: 80001}


class Runner:
    """Subprocess.run-compatible stub mapping commands to preset stdout."""

    def __init__(self, outputs: dict[str, object]):
        self.outputs = outputs
        self.commands: list[str] = []

    def __call__(self, argv, **kw):
        command = argv[-1]
        self.commands.append(command)

        class R:
            returncode = 0
            stderr = ""
            stdout = self.outputs[command] if isinstance(self.outputs[command], str) else json.dumps(self.outputs[command], ensure_ascii=False)
        return R()


@pytest.fixture
def snapshot_home(home):
    (home / "gateway.toml").write_text('''
[channels.telegram]
[snapshot]
sources = [
  { flow = "coursedump", command = "courses" },
  { flow = "researcher", command = "searches" },
]
[snapshot.scheduler]
command = "jobs"
''', "utf-8")
    return home


def test_snapshot_renders_flows_and_attaches_jobs_by_pgid(snapshot_home, capsys):
    cfg = load_config(snapshot_home)
    runner = Runner({"courses": COURSES, "searches": SEARCHES, "jobs": JOBS})
    now = datetime.datetime(2026, 8, 24, 7, 0, tzinfo=datetime.timezone.utc).astimezone()
    rc = snapshot(snapshot_home, cfg, print_only=True, runner=runner, now=now, pgid_of=PGIDS.get)
    assert rc == EXIT_OK
    text = capsys.readouterr().out
    local = now.strftime("%H:%M")
    end_1 = (now + datetime.timedelta(seconds=31000)).strftime("%H:%M")
    end_2 = (now + datetime.timedelta(seconds=3900)).strftime("%H:%M")
    assert text.splitlines() == [
        f"<b>[status] {local} · 4 workers</b>",
        "[coursedump] 2 workers · queue 2",
        "w1 maven-eteaiengbtcmp",
        "  End-to-End AI Engineering Bootcamp",
        f"  1/3 extract 52/83 · 1h01/~9h37 · ~{end_1} · stall 95m",
        "  windows 276",
        "w2 fm-harnessengagorws",
        f"  3/3 pages 3/23 · 0h20/? · ~{end_2} · running",
        "queue: bc-aiagentscourse, ab-tappliedaimc",
        "",
        "[researcher] 1 workers",
        "w1 sobes-otchety",
        "  Real senior Go engineer interview loop",
        "  1/3 cycles 3/24 · 1h08/? · running",
        "  judge: continue - not enough first-hand",
        "  claims 193 · sources 54 · map 10/10",
        "",
        "[scheduler] running 1",
        "nochnoj-sintez · running 0h18",
    ]


def test_snapshot_is_silent_when_idle_and_reports_broken_source(snapshot_home, capsys):
    cfg = load_config(snapshot_home)
    idle = {"snapshot": {"flow": "coursedump", "workers": [], "queue": []}}
    runner = Runner({"courses": idle, "searches": idle, "jobs": {"jobs": []}})
    assert snapshot(snapshot_home, cfg, print_only=True, runner=runner) == EXIT_OK
    assert "idle; no notification sent" in capsys.readouterr().out
    assert rows(snapshot_home)[-1]["reason"] == "idle"

    runner = Runner({"courses": "not json", "searches": idle, "jobs": {"jobs": []}})
    assert snapshot(snapshot_home, cfg, print_only=True, runner=runner) == EXIT_OK
    assert "? coursedump: not JSON" in capsys.readouterr().out


def test_snapshot_sends_status_envelope(snapshot_home):
    cfg = load_config(snapshot_home)
    runner = Runner({"courses": COURSES, "searches": SEARCHES, "jobs": JOBS})
    http = FakeHTTP()
    assert snapshot(snapshot_home, cfg, runner=runner, http=http, pgid_of=PGIDS.get) == EXIT_OK
    assert http.calls[0][1]["text"].startswith("<b>[status] ")
    assert rows(snapshot_home)[-1]["class"] == "status"


def test_fragment_tolerates_garbage_fields():
    flow = parse_fragment("x", {"snapshot": {"workers": [{"item": "a", "done": "12", "stage": "no"},
                                                        "junk", {"noitem": 1}], "queue": [{"item": "q"}]}})
    assert flow.name == "x" and len(flow.workers) == 1
    assert flow.workers[0].done is None and flow.workers[0].stage_name == ""
    assert flow.queue == [{"item": "q", "name": "", "state": "waiting"}]
    assert parse_fragment("x", {"nope": 1}).errors == ["missing snapshot key"]


def test_duration_format():
    assert fmt_duration(None) == "?"
    assert fmt_duration(59) == "0h00"
    assert fmt_duration(3660) == "1h01"
    assert fmt_duration(27 * 3600 + 5 * 60) == "1d03h"


def test_kind_hint_with_v2_flags_sets_class_and_live(home, monkeypatch):
    calls = []
    _patch_http(monkeypatch, calls)
    rc = main(["--home", str(home), "deliver", "--from", "researcher", "--about", "stavki",
               "--class", "", "--kind", "progress", "--title", "cycle 2/12",
               "--lines", "claims 193\nWhat to do: nothing"])
    assert rc == EXIT_OK
    row = rows(home)[-1]
    assert row["class"] == "event" and row["live"] is True


def test_action_removes_duplicate_what_to_do_line():
    env_ = validate(env(cls="alert", title="fail", lines=["reason", "What to do: scheduler run x"],
                        action="scheduler run x"), DEFAULT_CLASSES)
    assert env_.lines == ["reason"]
    assert render(env_).endswith("-> scheduler run x")
