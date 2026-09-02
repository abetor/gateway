"""P4-GW-01: versioned event stdin, argv target, receipt and replay contract."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import tomllib
from pathlib import Path

import pytest

from tool_gateway.cli import EXIT_FAIL, EXIT_OK, EXIT_TRANSIENT, main
from tool_gateway.core.channel import Inbound
from tool_gateway.core.config import EventTargetCfg, Project, TelegramCfg, load_config
from tool_gateway.ext.event.dispatch import (
    MAX_EVENT_BYTES,
    EventError,
    TargetProcessResult,
    dispatch_event,
    read_receipt,
    telegram_event,
    validate_event,
)
from tool_gateway.ext.telegram.route import Route
from tool_gateway.ext.telegram.serve import serve

from _helpers import FakeChannel, Run, fake_rig


ZERO_HASH = "0" * 64
ONE_HASH = "1" * 64


def _event(
    *,
    target: str = "runtime",
    text: str = "Summary complete",
    seed: bytes = b"stable-semantic-event",
) -> dict[str, object]:
    digest = hashlib.sha256(seed).hexdigest()
    return {
        "schema_version": 1,
        "event_id": "event_" + digest[:58],
        "dedupe_key": "sha256:" + digest,
        "target": target,
        "source": "runtime",
        "kind": "run-complete",
        "text": text,
        "project": None,
        "ref": None,
    }


def _target_receipt(event: dict[str, object], *, status: str = "delivered") -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "event_id": event["event_id"],
            "dedupe_key": event["dedupe_key"],
            "status": status,
            "effect_sha256": ONE_HASH,
        },
        separators=(",", ":"),
    ).encode()


class _Results:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.results.pop(0)


def _process(event, *, rc=0, status="delivered", timed_out=False):
    stdout = b"" if timed_out else _target_receipt(event, status=status)
    return TargetProcessResult(rc, stdout, b"", timed_out=timed_out)


def test_capabilities_exactly_match_runtime_p0_contract(capfd):
    assert main(["capabilities", "--json"]) == EXIT_OK
    captured = capfd.readouterr()
    payload = json.loads(captured.out)
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text("utf-8")
    )["project"]

    assert captured.err == ""
    assert project["version"] == payload["version"] == "0.2.0"
    assert payload == {
        "schema_version": 1,
        "tool": "gateway",
        "version": "0.2.0",
        "capabilities": [
            {
                "name": "dispatch-event",
                "argv": ["tool-gateway", "--home", "<home>", "dispatch-event", "--json"],
                "machine_output": {
                    "format": "json",
                    "schema_version": 1,
                    "required_fields": ["schema_version", "event_id", "status", "receipt"],
                },
                "exit_codes": {"0": "success", "1": "failure", "111": "transient"},
                "idempotency": "deduplicated",
            },
            {
                "name": "delivery-receipt",
                "argv": [
                    "tool-gateway",
                    "--home",
                    "<home>",
                    "receipt",
                    "<event-id>",
                    "--json",
                ],
                "machine_output": {
                    "format": "json",
                    "schema_version": 1,
                    "required_fields": ["schema_version", "event_id", "status", "dedupe_key"],
                },
                "exit_codes": {"0": "success", "1": "failure"},
                "idempotency": "read-only",
            },
            {
                "name": "deliver",
                "argv": [
                    "tool-gateway", "--home", "<home>", "deliver",
                    "--kind", "<progress|done|fail|stall|wait|info>",
                    "--source", "<source>", "--subject", "<subject>",
                    "--title", "<title>", "--lines", "<lines>",
                ],
                "input": {
                    "format": "argv-or-stdin",
                    "stdin": "first-line-title-rest-lines",
                    "kinds": ["progress", "done", "fail", "stall", "wait", "info"],
                },
                "output": {
                    "format": "text",
                    "values": [
                        "deliver: delivered (new)",
                        "deliver: delivered (edit)",
                        "deliver: delivered (dup)",
                    ],
                },
                "exit_codes": {"0": "success", "1": "failure", "111": "transient"},
                "idempotency": "deduplicated-30m",
            },
            {
                "name": "deliver-v2",
                "argv": [
                    "tool-gateway", "--home", "<home>", "deliver",
                    "--from", "<flow>", "--about", "<operation>", "--class",
                    "<status|event|alert|action|content|ack>", "--title", "<title>",
                    "--line", "<fact>", "--name", "<human name>", "--action", "<command>",
                    "--urgency", "<low|normal|high>", "--live",
                ],
                "input": {
                    "format": "argv-or-json-stdin",
                    "json_fields": ["from", "about", "class", "title", "lines", "name",
                                    "action", "urgency", "dedupe", "live", "at"],
                    "classes": ["ack", "action", "alert", "content", "event", "status"],
                },
                "output": {
                    "format": "text",
                    "values": [
                        "deliver: delivered (new)",
                        "deliver: delivered (edit)",
                        "deliver: delivered (dup)",
                        "deliver: deferred (<state>), pending drain",
                        "deliver: not delivered (network/platform), queued for retry (...)",
                    ],
                },
                "exit_codes": {"0": "success-or-queued", "1": "failure", "111": "transient"},
                "idempotency": "deduplicated-per-class",
            },
            {
                "name": "drain",
                "argv": ["tool-gateway", "--home", "<home>", "drain"],
                "machine_output": {"format": "json", "schema_version": 1, "required_fields": []},
                "exit_codes": {"0": "success", "1": "failure"},
                "idempotency": "idempotent",
            },
            {
                "name": "snapshot",
                "argv": ["tool-gateway", "--home", "<home>", "snapshot", "[--print]"],
                "machine_output": {"format": "json", "schema_version": 1, "required_fields": []},
                "exit_codes": {"0": "success-or-idle", "1": "failure"},
                "idempotency": "idempotent",
            },
        ],
    }


def test_target_config_is_strict_and_env_names_carry_no_values(tmp_path):
    (tmp_path / "gateway.toml").write_text(
        """
[targets.runtime]
argv = ["python3", "target.py"]
env = ["RUNTIME_TARGET_TOKEN"]
retries = 3
retry_delay_seconds = 0.25
timeout_seconds = 12
""",
        "utf-8",
    )
    target = load_config(tmp_path).targets["runtime"]
    assert target == EventTargetCfg(
        argv=("python3", "target.py"),
        env=("RUNTIME_TARGET_TOKEN",),
        retries=3,
        retry_delay_seconds=0.25,
        timeout_seconds=12.0,
    )

    invalid = [
        "argv = \"python3\"",
        "argv = [\"python3\\n\"]",
        "argv = [\"python3\"]\nenv = [\"TOKEN=test-value\"]",
        "argv = [\"python3\", \"--token=test-value\"]",
        "argv = [\"python3\"]\nretries = true",
        "argv = [\"python3\"]\nretry_delay_seconds = nan",
        "argv = [\"python3\"]\ntimeout_seconds = 3601",
        "argv = [\"python3\"]\nunknown = 1",
    ]
    for body in invalid:
        (tmp_path / "gateway.toml").write_text(f"[targets.runtime]\n{body}\n", "utf-8")
        with pytest.raises((ValueError, TypeError)):
            load_config(tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"event_id": "../event"},
        {"dedupe_key": "sha256:" + "f" * 64},
        {"text": "Authorization: Bearer test-value"},
        {"text": "see file:private-note"},
        {"text": "bad\ncontrol"},
        {"text": "bad\ud800surrogate"},
        {"ref": True},
        {"project": "unexpected"},
    ],
)
def test_event_schema_rejects_types_control_privacy_and_identity(change):
    event = {**_event(), **change}
    with pytest.raises(EventError):
        validate_event(event)


def test_dispatch_retries_transient_then_commits_one_receipt(tmp_path):
    event = _event()
    run = _Results(
        [
            TargetProcessResult(EXIT_TRANSIENT, b"private transient", b"secret"),
            TargetProcessResult(None, b"", b"private timeout", timed_out=True),
            _process(event),
        ]
    )
    sleeps = []
    target = EventTargetCfg(
        argv=("fake-target",), retries=2, retry_delay_seconds=0.5, timeout_seconds=1.0
    )

    result = dispatch_event(tmp_path, event, target, run=run, sleep=sleeps.append)

    assert result.exit_code == EXIT_OK
    assert result.response["status"] == "delivered"
    assert result.response["receipt"]["attempts"] == 3
    assert sleeps == [0.5, 0.5]
    assert len(run.calls) == 3
    assert all(call[1]["stdin"].endswith(b"\n") for call in run.calls)
    assert json.loads(run.calls[0][1]["stdin"]) == event
    assert read_receipt(tmp_path, str(event["event_id"])) == result.response["receipt"]
    assert len(list((tmp_path / "state/event-receipts").glob("event_*.json"))) == 1


def test_replay_skips_target_and_changed_content_conflicts(tmp_path):
    event = _event()
    run = _Results([_process(event)])
    target = EventTargetCfg(argv=("fake-target",))

    first = dispatch_event(tmp_path, event, target, run=run)
    second = dispatch_event(tmp_path, event, target, run=run)

    assert first.response["replayed"] is False
    assert second.response["replayed"] is True
    assert len(run.calls) == 1
    with pytest.raises(EventError, match="event_conflict"):
        dispatch_event(tmp_path, {**event, "text": "Different text"}, target, run=run)


def test_retryable_receipt_resumes_on_next_invocation(tmp_path):
    event = _event()
    transient = TargetProcessResult(EXIT_TRANSIENT, b"", b"")
    first_run = _Results([transient, transient])
    target = EventTargetCfg(argv=("fake-target",), retries=1)

    first = dispatch_event(tmp_path, event, target, run=first_run)
    assert first.exit_code == EXIT_TRANSIENT
    assert first.response["receipt"]["attempts"] == 2
    second_run = _Results([_process(event)])
    second = dispatch_event(tmp_path, event, target, run=second_run)
    assert second.exit_code == EXIT_OK
    assert second.response["receipt"]["attempts"] == 3


def test_failed_receipt_can_recover_but_delivered_stays_deduplicated(tmp_path):
    event = _event()
    target = EventTargetCfg(argv=("fake-target",), retries=0)
    failed_run = _Results([_process(event, rc=EXIT_FAIL, status="failed")])
    failed = dispatch_event(tmp_path, event, target, run=failed_run)
    assert failed.exit_code == EXIT_FAIL

    recovered_run = _Results([_process(event)])
    recovered = dispatch_event(tmp_path, event, target, run=recovered_run)
    replay = dispatch_event(tmp_path, event, target, run=recovered_run)
    assert recovered.exit_code == EXIT_OK
    assert recovered.response["receipt"]["attempts"] == 2
    assert replay.response["replayed"] is True
    assert len(recovered_run.calls) == 1


def test_exported_target_config_is_validated_before_effect(tmp_path):
    event = _event()
    run = _Results([_process(event)])
    with pytest.raises(EventError, match="invalid_target_config"):
        dispatch_event(
            tmp_path,
            event,
            EventTargetCfg(argv=("fake",), retries=True),
            run=run,
        )
    assert run.calls == []
    assert not (tmp_path / "state").exists()

    with pytest.raises(EventError, match="invalid_target_config"):
        dispatch_event(
            tmp_path,
            event,
            EventTargetCfg(argv=("fake",), timeout_seconds=10**10_000),
            run=run,
        )
    with pytest.raises(EventError, match="invalid_target_config"):
        dispatch_event(
            tmp_path,
            event,
            EventTargetCfg(argv=("bad\ud800surrogate",)),
            run=run,
        )


def test_receipt_lookup_is_read_only_when_missing(tmp_path):
    event = _event()
    assert read_receipt(tmp_path, str(event["event_id"])) is None
    assert not (tmp_path / "state").exists()


def test_receipt_payload_must_match_requested_filename(tmp_path, capfd):
    requested = _event(seed=b"requested")
    stored_event = _event(seed=b"stored")
    target = EventTargetCfg(argv=("fake-target",))
    dispatch_event(
        tmp_path,
        stored_event,
        target,
        run=_Results([_process(stored_event)]),
    )
    receipt_dir = tmp_path / "state/event-receipts"
    source = receipt_dir / f"{stored_event['event_id']}.json"
    mismatched = receipt_dir / f"{requested['event_id']}.json"
    mismatched.write_bytes(source.read_bytes())

    with pytest.raises(EventError, match="invalid_receipt_state"):
        read_receipt(tmp_path, str(requested["event_id"]))

    assert main(
        ["--home", str(tmp_path), "receipt", str(requested["event_id"]), "--json"]
    ) == EXIT_FAIL
    captured = capfd.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["error"] == {"code": "invalid_receipt_state"}
    assert "Traceback" not in captured.err


def test_invalid_or_large_target_output_is_generic_and_bounded(tmp_path):
    event = _event()
    secret = b"TEST-SECRET-OUTPUT"
    run = _Results(
        [TargetProcessResult(EXIT_OK, secret, secret, output_too_large=True)]
    )
    result = dispatch_event(tmp_path, event, EventTargetCfg(argv=("fake",)), run=run)
    rendered = json.dumps(result.response)
    assert result.exit_code == EXIT_FAIL
    assert result.diagnostic == "target_output_too_large"
    assert secret.decode() not in rendered
    assert len(rendered.encode()) < 4096


def test_live_fake_target_gets_stdin_selected_env_and_no_shell(tmp_path, capfd, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    script = tmp_path / "fake target.py"
    seen = tmp_path / "seen.json"
    shell_marker = tmp_path / "SHELL-MUST-NOT-RUN"
    literal = f"$(touch {shell_marker})"
    token = "test-target-token-value"
    script.write_text(
        """import hashlib, json, os, pathlib, sys
event = json.load(sys.stdin)
assert os.environ["TARGET_TOKEN"] == "test-target-token-value"
assert "test-target-token-value" not in json.dumps(event)
pathlib.Path(sys.argv[1]).write_text(json.dumps({"event": event, "literal": sys.argv[2]}))
print(json.dumps({
    "schema_version": 1,
    "event_id": event["event_id"],
    "dedupe_key": event["dedupe_key"],
    "status": "delivered",
    "effect_sha256": hashlib.sha256(b"accepted").hexdigest(),
}))
""",
        "utf-8",
    )
    argv = json.dumps([sys.executable, str(script), str(seen), literal])
    (home / "gateway.toml").write_text(
        f'[targets.runtime]\nargv = {argv}\nenv = ["TARGET_TOKEN"]\nretries = 0\n',
        "utf-8",
    )
    monkeypatch.setenv("TARGET_TOKEN", token)
    event = _event()
    monkeypatch.setattr(sys, "stdin", _BinaryInput(json.dumps(event).encode()))

    assert main(["--home", str(home), "dispatch-event", "--json"]) == EXIT_OK
    captured = capfd.readouterr()
    response = json.loads(captured.out)
    observed = json.loads(seen.read_text("utf-8"))

    assert response["status"] == "delivered"
    assert observed == {"event": event, "literal": literal}
    assert not shell_marker.exists()
    assert token not in captured.out + captured.err
    assert str(script) not in captured.out + captured.err

    assert main(["--home", str(home), "receipt", str(event["event_id"]), "--json"]) == EXIT_OK
    receipt_output = capfd.readouterr()
    assert json.loads(receipt_output.out)["dedupe_key"] == event["dedupe_key"]
    assert token not in receipt_output.out + receipt_output.err


class _BinaryInput:
    def __init__(self, data: bytes):
        from io import BytesIO

        self.buffer = BytesIO(data)


@pytest.mark.parametrize(
    "body",
    [
        "channels = []\n",
        "[channels]\ntelegram = []\n",
        "[channels.telegram]\n[projects]\nbad = []\n",
    ],
)
def test_wrong_toml_container_is_one_generic_machine_error(
    tmp_path, capfd, monkeypatch, body
):
    home = tmp_path / "private-home"
    home.mkdir()
    (home / "gateway.toml").write_text(body, "utf-8")
    monkeypatch.setattr(sys, "stdin", _BinaryInput(json.dumps(_event()).encode()))

    assert main(["--home", str(home), "dispatch-event", "--json"]) == EXIT_FAIL
    captured = capfd.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["error"] == {"code": "invalid_config"}
    assert len(captured.out.encode()) < 4096
    assert "Traceback" not in captured.err
    assert str(home) not in captured.out + captured.err


def test_cli_malformed_input_and_config_are_structured_without_paths(
    tmp_path, capfd, monkeypatch
):
    home = tmp_path / "private-home"
    home.mkdir()
    secret = "test-config-secret"
    (home / "gateway.toml").write_text(
        f'[targets.runtime]\nargv = "{secret}"\n', "utf-8"
    )
    monkeypatch.setattr(sys, "stdin", _BinaryInput(json.dumps(_event()).encode()))
    assert main(["--home", str(home), "dispatch-event", "--json"]) == EXIT_FAIL
    captured = capfd.readouterr()
    payload = json.loads(captured.out)
    assert payload["error"] == {"code": "invalid_config"}
    assert str(home) not in captured.out + captured.err
    assert secret not in captured.out + captured.err

    monkeypatch.setattr(sys, "stdin", _BinaryInput(b"{" + b"x" * MAX_EVENT_BYTES))
    assert main(["--home", str(home), "dispatch-event", "--json"]) == EXIT_FAIL
    captured = capfd.readouterr()
    assert json.loads(captured.out)["error"] == {"code": "input_too_large"}
    assert len(captured.out.encode()) < 4096


def test_telegram_generic_target_is_replayed_but_rig_path_is_unchanged(tmp_path):
    generic = Project(target="runtime")
    rig_root = fake_rig(tmp_path)
    rig_project = tmp_path / "rig-project"
    rig_project.mkdir()
    cfg = TelegramCfg(
        rig=rig_root,
        default="events",
        projects={
            "events": generic,
            "rigproj": Project(path=str(rig_project)),
        },
        token_env="TELEGRAM_BOT_TOKEN",
        chat_id_env="TELEGRAM_CHAT_ID",
    )
    target = EventTargetCfg(argv=("fake-target",))
    calls = []

    def target_run(argv, **kwargs):
        event = json.loads(kwargs["stdin"])
        calls.append(event)
        return TargetProcessResult(EXIT_OK, _target_receipt(event), b"")

    inbound = Inbound(text="event", raw={"update_id": 42})
    first = FakeChannel([inbound])
    serve(
        first,
        cfg,
        once=True,
        log=lambda *args: None,
        home=tmp_path,
        targets={"runtime": target},
        target_run=target_run,
    )
    second = FakeChannel([inbound])
    serve(
        second,
        cfg,
        once=True,
        log=lambda *args: None,
        home=tmp_path,
        targets={"runtime": target},
        target_run=target_run,
    )
    assert len(calls) == 1
    assert calls[0]["source"] == "telegram"
    assert calls[0]["project"] == "events"
    assert first.outbox == ["events · gateway: event delivered"]
    assert second.outbox == ["events · gateway: event delivered"]

    rig_channel = FakeChannel([Inbound(text="rigproj: ordinary rig task")])
    rig_run = Run(out="legacy rig response")
    serve(
        rig_channel,
        cfg,
        once=True,
        run=rig_run,
        log=lambda *args: None,
        home=tmp_path,
        targets={"runtime": target},
    )
    assert rig_channel.outbox == ["rigproj · legacy rig response"]
    assert rig_run.calls[0][0][2:] == [
        "rig",
        "--path",
        str(rig_project),
        "ordinary rig task",
    ]


@pytest.mark.parametrize("text", ["#4 continue", "?7 use the second option"])
def test_telegram_non_session_target_rejects_reply_and_answer_before_effect(tmp_path, text):
    cfg = TelegramCfg(
        rig="",
        default="events",
        projects={"events": Project(target="runtime")},
        token_env="TELEGRAM_BOT_TOKEN",
        chat_id_env="TELEGRAM_CHAT_ID",
    )
    channel = FakeChannel([Inbound(text=text, raw={"update_id": 52})])

    def target_run(*args, **kwargs):
        raise AssertionError("non-session route reached target")

    serve(
        channel,
        cfg,
        once=True,
        log=lambda *args: None,
        home=tmp_path,
        targets={"runtime": EventTargetCfg(argv=("fake-target",))},
        target_run=target_run,
    )

    assert len(channel.outbox) == 1
    assert "only a rig project" in channel.outbox[0]
    assert not (tmp_path / "state/event-receipts").exists()


def test_telegram_event_excludes_sender_and_raw_payload():
    route = Route(project="events", kind="task", text="continue")
    event = telegram_event(
        route,
        Inbound(
            text="#4 continue",
            sender="private-chat-id",
            raw={"update_id": 91, "token": "must-not-pass"},
        ),
        target="runtime",
    )
    rendered = json.dumps(event)
    assert "private-chat-id" not in rendered
    assert "must-not-pass" not in rendered
    assert event["ref"] is None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.01)
    return False


def test_run_target_bounds_descendant_that_inherits_parent_pipes(tmp_path):
    event = _event()
    pidfile = tmp_path / "descendant.pid"
    script = tmp_path / "target_with_descendant.py"
    script.write_text(
        """import json, pathlib, subprocess, sys
event = json.load(sys.stdin)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
print(json.dumps({
    "schema_version": 1,
    "event_id": event["event_id"],
    "dedupe_key": event["dedupe_key"],
    "status": "delivered",
    "effect_sha256": "1" * 64,
}), flush=True)
""",
        "utf-8",
    )
    from tool_gateway.ext.event.dispatch import run_target

    started = time.monotonic()
    result = run_target(
        (sys.executable, str(script), str(pidfile)),
        stdin=json.dumps(event).encode(),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=0.5,
    )
    elapsed = time.monotonic() - started
    descendant = int(pidfile.read_text("utf-8"))
    try:
        assert elapsed < 0.75
        assert result.timed_out is True
        assert result.returncode is None
        assert json.loads(result.stdout)["event_id"] == event["event_id"]
        assert _wait_dead(descendant)
    finally:
        if _alive(descendant):
            try:
                os.kill(descendant, 9)
            except OSError:
                pass


def test_selector_constructor_failure_reaps_child_and_closes_pipes(
    tmp_path, capfd, monkeypatch
):
    from tool_gateway.ext.event import dispatch as event_dispatch

    home = tmp_path / "private-home"
    home.mkdir()
    pidfile = tmp_path / "target.pid"
    script = tmp_path / "sleeping_target.py"
    script.write_text(
        """import os, pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(30)
""",
        "utf-8",
    )
    argv = json.dumps([sys.executable, str(script), str(pidfile)])
    (home / "gateway.toml").write_text(
        f'[targets.runtime]\nargv = {argv}\nretries = 0\ntimeout_seconds = 0.5\n',
        "utf-8",
    )

    real_popen = event_dispatch.subprocess.Popen
    observed = {}

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        observed["process"] = process
        return process

    def failing_selector():
        wait_until = time.monotonic() + 0.2
        while not pidfile.exists() and time.monotonic() < wait_until:
            time.sleep(0.005)
        raise OSError("private selector diagnostic /private/runtime/path")

    monkeypatch.setattr(event_dispatch.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(event_dispatch.selectors, "DefaultSelector", failing_selector)
    monkeypatch.setattr(sys, "stdin", _BinaryInput(json.dumps(_event()).encode()))

    before_fds = len(os.listdir("/dev/fd"))
    started = time.monotonic()
    assert main(["--home", str(home), "dispatch-event", "--json"]) == EXIT_FAIL
    elapsed = time.monotonic() - started
    captured = capfd.readouterr()
    process = observed["process"]

    try:
        assert elapsed < 0.75
        assert len(captured.out.splitlines()) == 1
        assert json.loads(captured.out)["error"] == {"code": "target_setup_failed"}
        assert "Traceback" not in captured.err
        assert "selector diagnostic" not in captured.out + captured.err
        assert str(home) not in captured.out + captured.err
        assert pidfile.exists()
        assert int(pidfile.read_text("utf-8")) == process.pid
        assert process.poll() is not None
        assert _wait_dead(process.pid)
        assert process.stdin.closed
        assert process.stdout.closed
        assert process.stderr.closed
        assert len(os.listdir("/dev/fd")) == before_fds
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
