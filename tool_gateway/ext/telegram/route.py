"""Route incoming Telegram messages to a session-target CLI.

Supported user syntax:

``<text>``
    Create a task in the default project.
``name: <text>``
    Create a task in the registered project named ``name``. An unregistered
    prefix remains ordinary text.
``#N <text>``
    Reply to session N through ``rig --reply N``.
``?M <text>``
    Answer question M through ``rig --answer M``.
reply to a notification
    Derive the session and project from its ``rig #N · project · flow`` address.

Addressing fails closed because session and message IDs are allocated per
project and therefore collide. With multiple projects, ``#N`` or ``?M`` must
have an unambiguous project prefix or notification address. The default project
applies only to new tasks.

Dispatch is serialized per project in ``serve.py``. A CLI call is synchronous,
so a long flow is a long turn. CLI failure does not stop polling; its bounded
stderr tail is returned to the operator.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from ...core.channel import Inbound
from ...core.config import TelegramCfg

CLI_TIMEOUT = 3600.0    # Upper bound for one session-target CLI turn.


@dataclass
class Route:
    """Parsed destination and action: task, reply, or answer with an optional ID."""
    project: str
    kind: str
    text: str
    ref: Optional[int] = None


# A separator after ``#N`` or ``?M`` is whitespace or light punctuation. A
# greedy hyphen class would silently corrupt text such as ``#12-3 tickets``.
_SID_LEAD = re.compile(r"^\s*(?:rig\s*)?#(\d+)(?:\s+|[:,.]\s*|$)", re.I)
_MSG_LEAD = re.compile(r"^\s*\?\s*(\d+)(?:\s+|[:,.]\s*|$)")
# A session notification starts with ``rig #<sid> · <project> · <flow>``. Match
# only the first reply-to line so references in facts cannot be mistaken for the address.
_ADDR = re.compile(r"^rig\s*#(\d+)(?:\s*·\s*(\S+))?", re.I)
# A bare approval without correlation is probably intended for an existing
# session. Starting a new task from it wastes work, so ask for an address.
_BARE_APPROVAL = re.compile(
    r"^\s*(\u043e\u043a|ok|\u043e\u043a\u0435\u0439|okay|\u0433\u043e|go|\u0434\u0430|yes|y|approve|"
    r"\u0430\u043f\u0440\u0443\u0432|\u043e\u0434\u043e\u0431\u0440\u044f\u044e|"
    r"\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0430\u044e|lgtm|\u0430\u0433\u0430|"
    r"\u0434\u0430\u0432\u0430\u0439|\u043f\u043e\u0435\u0445\u0430\u043b\u0438|\+)"
    r"[\s!.]*$", re.I)


def _project_prefix(text: str, cfg: TelegramCfg):
    """Split a registered ``name: ...`` prefix; otherwise preserve the text."""
    head, sep, rest = text.partition(":")
    if sep and head.strip() in cfg.projects:
        return head.strip(), rest.strip()
    return "", text


def _project_by_name(name: str, cfg: TelegramCfg) -> str:
    """Resolve a notification address name by registry key or path basename."""
    if name in cfg.projects:
        return name
    for key, proj in cfg.projects.items():
        if proj.path.rstrip("/").rsplit("/", 1)[-1] == name:
            return key
    return ""


def parse_route(msg: Inbound, cfg: TelegramCfg):
    """Return a route or a user-facing refusal without invoking the CLI.

    Precedence is explicit project prefix, explicit ``#N``/``?M``, reply-to
    correlation, then new task.
    """
    project, text = _project_prefix(msg.text.strip(), cfg)

    kind, ref = "task", None
    m = _SID_LEAD.match(text)
    if m:
        kind, ref, text = "reply", int(m.group(1)), text[m.end():].strip()
    else:
        m = _MSG_LEAD.match(text)
        if m:
            kind, ref, text = "answer", int(m.group(1)), text[m.end():].strip()

    if msg.reply_to_text:                          # Derive session/project from notification address.
        m = _ADDR.match(msg.reply_to_text.split("\n", 1)[0].strip())
        if m:
            if kind == "task":                     # Explicit IDs in text take precedence.
                kind, ref = "reply", int(m.group(1))
            addr_name = m.group(2) or ""
            if addr_name:
                addr_project = _project_by_name(addr_name, cfg)
                if not addr_project:
                    return (f"gateway: notification project {addr_name!r} is not registered "
                            f"(available: {', '.join(sorted(cfg.projects))})")
                if project and project != addr_project:   # Prevent cross-project session correlation.
                    return (f"gateway: prefix selects {project!r}, but notification selects "
                            f"{addr_project!r}; specify one address")
                project = addr_project

    if not text:
        return "gateway: empty message; provide task or reply text"
    if kind == "task" and _BARE_APPROVAL.match(text):
        return ("gateway: a bare approval has no session address; reply to a `rig #N` "
                "notification or start with `#N`")
    if not project:
        if kind == "task":
            project = cfg.default
            if not project:
                return ("gateway: project is ambiguous; start with a registered prefix "
                        f"`name: ...` (available: {', '.join(sorted(cfg.projects))})")
        elif len(cfg.projects) == 1:
            project = next(iter(cfg.projects))
        else:
            # Session and message IDs are project-local. Falling back to the
            # default could reach another project's session, so fail closed.
            return (f"gateway: project for #{ref} is ambiguous; multiple projects are registered: "
                    f"use `name: ...` (available: {', '.join(sorted(cfg.projects))})")
    if cfg.projects[project].target != "rig" and kind != "task":
        return (f"gateway: project {project!r} uses a non-session event target; "
                "only a rig project supports `#N` replies and `?M` answers")
    return Route(project=project, kind=kind, text=text, ref=ref)


def rig_cmd(route: Route, cfg: TelegramCfg) -> list:
    """Build rig CLI argv and append project passthrough arguments unchanged."""
    proj = cfg.projects[route.project]
    base = [sys.executable, "-m", "rig", "--path", proj.path]
    core = {"task": [route.text],
            "reply": ["--reply", str(route.ref), route.text],
            "answer": ["--answer", str(route.ref), route.text]}[route.kind]
    return base + core + list(proj.args)


def _run_group(cmd: list, *, cwd: str, timeout: float):
    """Run a subprocess in its own process group with ``subprocess.run`` semantics.

    A timeout kills the entire group, including spawned workers, rather than
    leaving descendants running without supervision.
    """
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, errors="replace", start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.wait()
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def _final_notify_delivered(out: str) -> bool:
    """Return whether the final run notification was delivered.

    Session targets may report each notification in English or in the legacy
    localized form. An earlier successful plan notification must not suppress
    fallback output when the final notification failed.
    """
    legacy_notice = "\u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u0435 ["
    legacy_sent = "\u043f\u043e\u0441\u043b\u0430\u043d\u043e"
    for line in reversed(out.splitlines()):
        folded = line.casefold()
        if "notification [" in folded:
            return "sent" in folded
        if legacy_notice in folded:
            return legacy_sent in folded
    return False


def dispatch(route: Route, cfg: TelegramCfg, *, run: Optional[Callable] = None,
             timeout: float = CLI_TIMEOUT, events: Optional[Callable] = None) -> str:
    """Run one route synchronously and return text for the operator.

    The event callback records argv and exit status in the gateway dispatch log.
    """
    run = run or _run_group
    emit = events or (lambda stream, **row: None)
    cmd = rig_cmd(route, cfg)
    tag = f"{route.project} · " if len(cfg.projects) > 1 else ""
    try:
        r = run(cmd, cwd=cfg.rig, timeout=timeout)
    except subprocess.TimeoutExpired:
        emit("dispatch", channel="telegram", project=route.project, kind=route.kind,
             argv=cmd, exit=None, timeout=True)
        return (f"{tag}gateway: turn exceeded {int(timeout)}s; process group was killed, "
                f"inspect the session with `rig --list`")
    emit("dispatch", channel="telegram", project=route.project, kind=route.kind,
         argv=cmd, exit=r.returncode)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0 and err:
        out = (out + "\n" if out else "") + "[stderr] " + err[-500:]
        return tag + out
    # If the final flow notification arrived, stay silent because echoing stdout
    # would duplicate the operator's terminal message.
    if r.returncode == 0 and _final_notify_delivered(out):
        return ""
    return tag + (out or f"gateway: CLI returned no output (exit {r.returncode})")
