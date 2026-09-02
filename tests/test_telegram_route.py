"""Telegram ingress routing and dispatch to the rig CLI.

Coverage includes project prefixes, ``#N`` replies, ``?M`` answers, reply-to
correlation, bare approvals, fail-closed multi-project addressing, argv and cwd
construction, visible stderr, notification echo suppression, audit events, and
whole-process-group timeout cleanup.

Subprocess execution is injected everywhere except the local ``_run_group``
test, which uses ``/bin/sh`` and ``sleep`` without network or paid services.
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest

from _helpers import Run, tcfg
from tool_gateway.core.channel import Inbound
from tool_gateway.ext.telegram import route as dp


# ===================================================================== routing
def test_route_plain_text_goes_to_default(tmp_path):
    cfg = tcfg(tmp_path)
    r = dp.parse_route(Inbound(text="add search"), cfg)
    assert (r.project, r.kind, r.text, r.ref) == ("alpha", "task", "add search", None)
    # ``#N`` away from the start is content, not a session address.
    assert dp.parse_route(Inbound(text="fix the bug from ticket #42"), cfg).kind == "task"


def test_route_project_prefix_and_non_registry_colon(tmp_path):
    cfg = tcfg(tmp_path, projects=("alpha", "beta"), default="alpha")
    r = dp.parse_route(Inbound(text="beta: #7 approve"), cfg)
    assert (r.project, r.kind, r.ref, r.text) == ("beta", "reply", 7, "approve")
    # An unregistered colon prefix remains ordinary task text.
    r2 = dp.parse_route(Inbound(text="refactor: extract function"), cfg)
    assert (r2.project, r2.kind, r2.text) == ("alpha", "task", "refactor: extract function")


def test_route_sid_and_msg_prefixes(tmp_path):
    cfg = tcfg(tmp_path)
    r = dp.parse_route(Inbound(text="rig #12: split into two units"), cfg)
    assert (r.kind, r.ref, r.text) == ("reply", 12, "split into two units")
    a = dp.parse_route(Inbound(text="?5 use the queue"), cfg)
    assert (a.kind, a.ref, a.text) == ("answer", 5, "use the queue")


def test_route_reply_to_notify_takes_sid_and_project(tmp_path):
    cfg = tcfg(tmp_path, projects=("alpha", "beta"), default="alpha")
    notify = "rig #33 · beta · lead\nANSWER REQUIRED: plan ready"
    r = dp.parse_route(Inbound(text="approve", reply_to_text=notify), cfg)
    assert (r.project, r.kind, r.ref, r.text) == ("beta", "reply", 33, "approve")
    # A notification project also resolves by the registered path basename.
    cfg.projects["beta"].path = "/x/y/myproject"
    r2 = dp.parse_route(Inbound(text="approve", reply_to_text="rig #4 · myproject · lead"), cfg)
    assert (r2.project, r2.ref) == ("beta", 4)
    # An unknown notification project is refused instead of silently defaulting.
    out = dp.parse_route(Inbound(text="approve", reply_to_text="rig #9 · ghost · lead"), cfg)
    assert isinstance(out, str) and "ghost" in out
    # Only the first line is an address; session references in the body are facts.
    r4 = dp.parse_route(Inbound(text="approve",
                                reply_to_text="rig #33 · beta · lead\nsee rig #7 in facts"), cfg)
    assert (r4.project, r4.ref) == ("beta", 33)


def test_route_no_project_address_fail_closed_at_multi(tmp_path):
    """Project-local IDs require an unambiguous project in a multi-project registry."""
    multi = tcfg(tmp_path, projects=("alpha", "beta"), default="alpha")
    # Legacy notification without a project in its address.
    out = dp.parse_route(Inbound(text="approve", reply_to_text="rig #2: session complete"), multi)
    assert isinstance(out, str) and "#2" in out and "alpha" in out
    # Explicit IDs without a project prefix are equally ambiguous.
    assert isinstance(dp.parse_route(Inbound(text="#5 approve"), multi), str)
    assert isinstance(dp.parse_route(Inbound(text="?12 via Y"), multi), str)
    # A single project is unambiguous.
    single = tcfg(tmp_path)
    r = dp.parse_route(Inbound(text="approve", reply_to_text="rig #2: session complete"), single)
    assert (r.project, r.ref) == ("alpha", 2)
    # A new task still uses the configured default.
    r2 = dp.parse_route(Inbound(text="add feature"), multi)
    assert (r2.project, r2.kind) == ("alpha", "task")


def test_route_prefix_vs_notify_project_mismatch_asks(tmp_path):
    """A beta prefix on an alpha notification is refused as cross-project correlation."""
    cfg = tcfg(tmp_path, projects=("alpha", "beta"), default="alpha")
    out = dp.parse_route(Inbound(text="beta: approve", reply_to_text="rig #33 · alpha · lead"), cfg)
    assert isinstance(out, str) and "alpha" in out and "beta" in out


def test_route_sid_marker_requires_clean_separator(tmp_path):
    """A hyphen after an ID must not turn ordinary task text into a reply."""
    cfg = tcfg(tmp_path)
    r = dp.parse_route(Inbound(text="#12-3 make tickets"), cfg)
    assert r.kind == "task" and r.text == "#12-3 make tickets"
    r2 = dp.parse_route(Inbound(text="#5 --approve"), cfg)   # Preserve the reply text.
    assert (r2.kind, r2.ref, r2.text) == ("reply", 5, "--approve")


def test_route_answer_on_reply_to_notify(tmp_path):
    """An explicit ``?M`` reply uses the notification project but remains an answer."""
    cfg = tcfg(tmp_path, projects=("alpha", "beta"), default="alpha")
    r = dp.parse_route(Inbound(text="?12 use Y",
                               reply_to_text="rig #33 · beta · lead"), cfg)
    assert (r.project, r.kind, r.ref, r.text) == ("beta", "answer", 12, "use Y")


def test_route_guards(tmp_path):
    cfg = tcfg(tmp_path)
    assert "empty" in dp.parse_route(Inbound(text="#5"), cfg)
    assert "no session address" in dp.parse_route(Inbound(text="ok"), cfg)
    multi = tcfg(tmp_path, projects=("alpha", "beta"))
    out = dp.parse_route(Inbound(text="add feature"), multi)
    assert isinstance(out, str) and "alpha" in out and "beta" in out   # Show options without a default.


# ===================================================================== dispatch -> CLI
def test_dispatch_builds_argv_and_cwd(tmp_path):
    cfg = tcfg(tmp_path)
    cfg.projects["alpha"].args = ["--worker", "codex"]
    run = Run()
    out = dp.dispatch(dp.Route(project="alpha", kind="task", text="new feature"), cfg, run=run)
    cmd, kw = run.calls[0]
    assert cmd[2:] == ["rig", "--path", cfg.projects["alpha"].path, "new feature",
                       "--worker", "codex"]
    assert kw["cwd"] == cfg.rig and out == "READY"
    dp.dispatch(dp.Route(project="alpha", kind="reply", ref=5, text="approve"), cfg, run=run)
    assert run.calls[1][0][5:8] == ["--reply", "5", "approve"]
    dp.dispatch(dp.Route(project="alpha", kind="answer", ref=7, text="via Y"), cfg, run=run)
    assert run.calls[2][0][5:8] == ["--answer", "7", "via Y"]


def test_dispatch_surfaces_stderr_and_tags_multiproject(tmp_path):
    cfg = tcfg(tmp_path, projects=("alpha", "beta"), default="alpha")
    run = Run(rc=1, out="", err="rig reply · session #9 missing")
    out = dp.dispatch(dp.Route(project="beta", kind="reply", ref=9, text="approve"), cfg, run=run)
    assert "session #9 missing" in out and out.startswith("beta · ")  # Preserve cause and address.


def test_dispatch_silent_when_flow_notify_delivered(tmp_path):
    """Successful CLI plus delivered final notice suppresses duplicate stdout."""
    cfg = tcfg(tmp_path)
    run = Run(out="rig lead · done\n  notification [telegram]: sent")
    assert dp.dispatch(dp.Route(project="alpha", kind="task", text="x"), cfg, run=run) == ""
    run2 = Run(out="rig lead · done\nFINAL-REPORT: x")            # Missing notice keeps stdout fallback.
    assert "FINAL-REPORT" in dp.dispatch(
        dp.Route(project="alpha", kind="task", text="x"), cfg, run=run2)
    # Only the final notification result controls suppression.
    run3 = Run(out="notification [telegram]: sent\nFINAL-REPORT: y\n"
                   "notification [telegram]: not delivered - network")
    assert "FINAL-REPORT" in dp.dispatch(
        dp.Route(project="alpha", kind="task", text="x"), cfg, run=run3)


def test_dispatch_timeout_reports_group_kill(tmp_path):
    cfg = tcfg(tmp_path)

    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    out = dp.dispatch(dp.Route(project="alpha", kind="task", text="x"), cfg, run=run, timeout=1)
    assert "exceeded" in out and "rig --list" in out


def test_dispatch_logs_argv_and_exit(tmp_path):
    """Every dispatch records argv and exit status in the shared audit log."""
    cfg = tcfg(tmp_path)
    events = []
    dp.dispatch(dp.Route(project="alpha", kind="task", text="x"), cfg, run=Run(rc=3),
                events=lambda stream, **row: events.append((stream, row)))
    stream, row = events[0]
    assert stream == "dispatch" and row["channel"] == "telegram"
    assert row["exit"] == 3 and "rig" in row["argv"] and row["project"] == "alpha"

    events.clear()

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    dp.dispatch(dp.Route(project="alpha", kind="task", text="x"), cfg, run=boom, timeout=1,
                events=lambda stream, **row: events.append((stream, row)))
    assert events[0][1]["timeout"] is True and events[0][1]["exit"] is None


# ============================================================== kill process group
def _alive(pid: int) -> bool:
    """Probe whether a process is alive using signal zero."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                        # A reused foreign PID is treated as alive.
        return True
    return True


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    """Poll while init reaps an orphaned descendant instead of sleeping blindly."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_run_group_timeout_kills_grandchildren(tmp_path):
    """A turn timeout kills the whole process group, not only its direct child.

    The real subprocess test has ``sh`` spawn a background ``sleep`` and then
    sleep itself. Both must be dead after timeout. Replacing ``os.killpg`` with
    ``p.kill`` leaves the descendant alive and makes this test fail.
    """
    pidfile = tmp_path / "grandchild.pid"
    # The descendant writes its PID while the parent remains alive until timeout.
    script = f"sleep 30 & echo $! > {pidfile}; sleep 30"

    with pytest.raises(subprocess.TimeoutExpired):
        dp._run_group(["/bin/sh", "-c", script], cwd=str(tmp_path), timeout=1.0)

    assert pidfile.is_file(), "descendant did not start; the test proves nothing"
    grandchild = int(pidfile.read_text("utf-8").strip())
    try:
        assert _wait_dead(grandchild), (
            f"descendant {grandchild} survived timeout; child was killed instead of group")
    finally:
        if _alive(grandchild):                     # Do not leave an orphaned sleep process.
            try:
                os.kill(grandchild, 9)
            except OSError:
                pass
