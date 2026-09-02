"""Gateway configuration in ``<home>/gateway.toml``.

The schema contains channels and route, project, and target registries. It is
fail-closed: unknown sections and fields are errors rather than silent no-ops.
The file stores only environment variable names such as ``token_env``, never
secret values. ``load_env`` merges ``<home>/.env`` into the process environment,
while explicitly exported process variables take precedence. Secret values do
not enter configuration or argv.

[channels.webhook]                  # HTTP ingress: POST /hook/<route>
host = "127.0.0.1"                  # expose externally only by deliberate choice
port = 8787
token_env = "GATEWAY_TOKEN"         # environment variable NAME; value is at least 16 chars

[routes.build-complete]             # webhook route registry
command = "..."                     # receives GATEWAY_ROUTE and GATEWAY_PAYLOAD

[channels.telegram]                 # polling ingress and delivery channel
token_env = "TELEGRAM_BOT_TOKEN"    # these defaults may be omitted
chat_id_env = "TELEGRAM_CHAT_ID"
rig = "/absolute/path/to/rig"       # session target; required only by watch
default = "rig"                     # deliver needs only the two credential names above
                                    # default handles new tasks; omit it for one project

[projects.rig]                      # Telegram project registry, used only by watch
path = "/absolute/path/to/project"
args = ["--worker", "codex"]        # passthrough arguments for the session target CLI

[targets.runtime]                   # generic machine target: argv only, no shell
argv = ["python3", "/abs/target.py"]
env = ["RUNTIME_TARGET_TOKEN"]      # names only; values come from the environment
retries = 2
retry_delay_seconds = 1
timeout_seconds = 60

[projects.events]                   # Telegram can address a generic target without rig/path
target = "runtime"

[channels.linear]                   # placeholder: validated, but not implemented
token_env = "LINEAR_API_KEY"
workspace = ""

[notify]                            # version 2 envelope delivery policy
availability = ["mon 00:00-09:50 sleep", ...]  # empty means do not defer
defer_states = ["sleep", "gym"]     # normal/low waits under <home>/state/queue here
defer_max_age = "12h"               # older items are logged as dropped
retry_max_age = "6h"                # maximum network-failure retry age
[notify.classes.alert]              # override or add a class
dedupe_seconds = 7200

[snapshot]                          # collect local process state into a status envelope
timeout_seconds = 90
sources = [                         # commands returning JSON with a snapshot key
  { flow = "example", command = "example-tool status --json" },
]
[snapshot.scheduler]                # optional scheduler process metadata for correlation
command = "scheduler-tool --home /path/to/data list --json"
"""
from __future__ import annotations

import math
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from ..shared.config import load_toml

CONFIG_NAME = "gateway.toml"
# Default envelope class registry. It lives in the core for ``ext`` to consume;
# ``[notify.classes.<name>]`` may override fields or add classes.
DEFAULT_CLASSES: dict[str, dict] = {
    "status": {"dedupe_seconds": 0, "defer": True},
    "event": {"dedupe_seconds": 1800, "defer": True},
    "alert": {"dedupe_seconds": 7200, "defer": True},
    "action": {"dedupe_seconds": 7200, "defer": False},
    "content": {"dedupe_seconds": 3600, "defer": True},
    "ack": {"dedupe_seconds": 600, "defer": True},
}
_MAX_DEDUPE_SECONDS = 7 * 24 * 3600
_MAX_AGE_SECONDS = 30 * 24 * 3600.0
_MAX_SNAPSHOT_SOURCES = 32
_DURATION = re.compile(r"^(\d+)([smhd])$")
_DURATION_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MIN_TOKEN = 16
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_MAX_ARGV_ITEMS = 32
_MAX_ARG_BYTES = 4096
_MAX_ARGV_BYTES = 16 * 1024
_MAX_RETRIES = 8
_MAX_RETRY_DELAY_SECONDS = 60.0
_MAX_TIMEOUT_SECONDS = 3600.0
_SECRET_ARG = re.compile(
    r"(?i)(?:^--?(?:api[-_]?key|authorization|password|secret|token)(?:=|$)|"
    r"\b(?:bearer|basic)\s+\S+|\bsk-[a-z0-9_-]{8,})"
)


@dataclass
class Project:
    path: str = ""
    args: list = field(default_factory=list)
    target: str = "rig"


@dataclass(frozen=True)
class EventTargetCfg:
    """Configured argv target whose credentials are selected only by env name."""

    argv: tuple[str, ...]
    env: tuple[str, ...] = ()
    retries: int = 2
    retry_delay_seconds: float = 0.0
    timeout_seconds: float = 60.0


@dataclass
class WebhookCfg:
    host: str
    port: int
    token_env: str
    routes: dict            # name -> command


@dataclass
class TelegramCfg:
    rig: str                # session target root; empty means watch ingress is not configured
    default: str            # default project; empty means new tasks require a prefix
    projects: dict          # name -> Project; empty means watch has nothing to spawn
    token_env: str
    chat_id_env: str


@dataclass
class LinearCfg:
    """Validated placeholder configuration for an unimplemented channel."""
    token_env: str
    workspace: str


@dataclass(frozen=True)
class NotifyCfg:
    """Version 2 envelope policy: classes, availability windows, and queue."""

    classes: dict                       # class -> {"dedupe_seconds": int, "defer": bool}
    availability: tuple[str, ...] = ()  # grid rows such as "mon 00:00-09:50 sleep"
    defer_states: tuple[str, ...] = ("sleep", "gym")
    defer_max_age_seconds: float = 12 * 3600.0
    retry_max_age_seconds: float = 6 * 3600.0


@dataclass(frozen=True)
class SnapshotSource:
    flow: str
    command: str                        # shell command returning JSON with a snapshot key


@dataclass(frozen=True)
class SnapshotCfg:
    sources: tuple[SnapshotSource, ...]
    scheduler_command: str = ""         # optional ``scheduler list --json`` command
    timeout_seconds: float = 60.0


@dataclass
class Config:
    """A ``None`` channel is disabled because its TOML section is absent."""
    webhook: WebhookCfg | None
    telegram: TelegramCfg | None
    linear: LinearCfg | None
    targets: dict[str, EventTargetCfg]
    notify: NotifyCfg = field(default_factory=lambda: NotifyCfg(classes=dict(DEFAULT_CLASSES)))
    snapshot: SnapshotCfg | None = None


def load_config(home: str | Path) -> Config:
    raw = load_toml(
        Path(home) / CONFIG_NAME,
        known={"channels", "routes", "projects", "targets", "notify", "snapshot"},
    )
    channels = _table(raw.get("channels"), "[channels]")
    unknown = set(channels) - {"webhook", "telegram", "linear"}
    if unknown:
        raise ValueError(f"[channels]: unknown channels {sorted(unknown)}")

    targets = _targets(raw.get("targets"))
    webhook = _webhook(channels.get("webhook"), raw.get("routes"))
    telegram = _telegram(channels.get("telegram"), raw.get("projects"), targets)
    linear = _linear(channels.get("linear"))
    if "routes" in raw and webhook is None:
        raise ValueError("[routes.*] requires [channels.webhook]")
    if "projects" in raw and telegram is None:
        raise ValueError("[projects.*] requires [channels.telegram]")
    if webhook is None and telegram is None and not targets:
        raise ValueError("gateway.toml: enable [channels.webhook], "
                         "[channels.telegram], or at least one [targets.*]; "
                         "linear is only a placeholder")
    return Config(webhook=webhook, telegram=telegram, linear=linear, targets=targets,
                  notify=_notify(raw.get("notify")), snapshot=_snapshot(raw.get("snapshot")))


def _duration(value: object, label: str) -> float:
    """Convert ``10m``, ``6h``, ``2d``, or a numeric value to seconds."""
    if type(value) in {int, float}:
        return _finite_number(value, label, minimum=0.0, maximum=_MAX_AGE_SECONDS)
    if isinstance(value, str):
        match = _DURATION.fullmatch(value.strip())
        if match is not None:
            seconds = int(match.group(1)) * _DURATION_MULT[match.group(2)]
            return _finite_number(seconds, label, minimum=0.0, maximum=_MAX_AGE_SECONDS)
    raise ValueError(f"{label}: expected a duration such as \"10m\"/\"6h\" or seconds")


def _notify(raw) -> "NotifyCfg":
    body = _table(raw, "[notify]")
    unknown = set(body) - {"classes", "availability", "defer_states", "defer_max_age",
                           "retry_max_age"}
    if unknown:
        raise ValueError(f"[notify]: unknown fields {sorted(unknown)}")
    classes = {name: dict(policy) for name, policy in DEFAULT_CLASSES.items()}
    for raw_name, policy in _table(body.get("classes"), "[notify.classes]").items():
        name = _name(raw_name, f"[notify.classes.{raw_name}]")
        policy = _table(policy, f"[notify.classes.{name}]")
        unknown = set(policy) - {"dedupe_seconds", "defer"}
        if unknown:
            raise ValueError(f"[notify.classes.{name}]: unknown fields {sorted(unknown)}")
        merged = dict(classes.get(name, {"dedupe_seconds": 0, "defer": True}))
        if "dedupe_seconds" in policy:
            seconds = policy["dedupe_seconds"]
            if type(seconds) is not int or not 0 <= seconds <= _MAX_DEDUPE_SECONDS:
                raise ValueError(f"[notify.classes.{name}].dedupe_seconds: integer 0..{_MAX_DEDUPE_SECONDS}")
            merged["dedupe_seconds"] = seconds
        if "defer" in policy:
            if type(policy["defer"]) is not bool:
                raise ValueError(f"[notify.classes.{name}].defer: true/false")
            merged["defer"] = policy["defer"]
        classes[name] = merged
    availability = body.get("availability", [])
    if not isinstance(availability, list) or not all(isinstance(row, str) for row in availability):
        raise ValueError("[notify].availability: expected a list of grid rows")
    defer_states = body.get("defer_states", ["sleep", "gym"])
    if (not isinstance(defer_states, list)
            or not all(isinstance(item, str) and _NAME.fullmatch(item) for item in defer_states)):
        raise ValueError("[notify].defer_states: expected a list of state names")
    return NotifyCfg(
        classes=classes, availability=tuple(availability), defer_states=tuple(defer_states),
        defer_max_age_seconds=_duration(body.get("defer_max_age", "12h"), "[notify].defer_max_age"),
        retry_max_age_seconds=_duration(body.get("retry_max_age", "6h"), "[notify].retry_max_age"),
    )


def _snapshot(raw) -> "SnapshotCfg | None":
    if raw is None:
        return None
    body = _table(raw, "[snapshot]")
    unknown = set(body) - {"sources", "scheduler", "timeout_seconds"}
    if unknown:
        raise ValueError(f"[snapshot]: unknown fields {sorted(unknown)}")
    sources_raw = body.get("sources", [])
    if not isinstance(sources_raw, list) or len(sources_raw) > _MAX_SNAPSHOT_SOURCES:
        raise ValueError(f"[snapshot].sources: expected at most {_MAX_SNAPSHOT_SOURCES} tables")
    sources = []
    for index, item in enumerate(sources_raw):
        item = _table(item, f"[[snapshot.sources]] #{index + 1}")
        unknown = set(item) - {"flow", "command"}
        if unknown:
            raise ValueError(f"[[snapshot.sources]] #{index + 1}: unknown fields {sorted(unknown)}")
        flow = _name(item.get("flow"), f"[[snapshot.sources]] #{index + 1}.flow")
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"[[snapshot.sources]] #{index + 1}: command is required")
        sources.append(SnapshotSource(flow=flow, command=command.strip()))
    scheduler = body.get("scheduler", {})
    scheduler = _table(scheduler, "[snapshot.scheduler]")
    unknown = set(scheduler) - {"command"}
    if unknown:
        raise ValueError(f"[snapshot.scheduler]: unknown fields {sorted(unknown)}")
    scheduler_command = scheduler.get("command", "")
    if not isinstance(scheduler_command, str):
        raise ValueError("[snapshot.scheduler].command: expected a string")
    timeout = _finite_number(body.get("timeout_seconds", 60.0), "[snapshot].timeout_seconds",
                             minimum=1.0, maximum=_MAX_TIMEOUT_SECONDS)
    return SnapshotCfg(sources=tuple(sources), scheduler_command=scheduler_command.strip(),
                       timeout_seconds=timeout)


def _has_control(value: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C")
        or unicodedata.category(character) in {"Zl", "Zp"}
        for character in value
    )


def _table(value: object, label: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label}: expected a table")
    return value


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise ValueError(f"{label}: name must match {_NAME.pattern}")
    return value


def _valid_target_arg(value: str) -> bool:
    try:
        raw = value.encode("utf-8")
    except UnicodeError:
        return False
    return (
        len(raw) <= _MAX_ARG_BYTES
        and value == unicodedata.normalize("NFC", value)
        and not _has_control(value)
        and _SECRET_ARG.search(value) is None
    )


def _finite_number(value: object, label: str, *, minimum: float, maximum: float) -> float:
    if type(value) not in {int, float}:
        raise ValueError(f"{label}: expected a finite number")
    try:
        checked = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{label}: expected a finite number") from None
    if not math.isfinite(checked):
        raise ValueError(f"{label}: expected a finite number")
    if not minimum <= checked <= maximum:
        raise ValueError(f"{label}: expected a value from {minimum:g} to {maximum:g}")
    return checked


def _targets(raw) -> dict[str, EventTargetCfg]:
    raw = _table(raw, "[targets]")
    targets: dict[str, EventTargetCfg] = {}
    for raw_name, body in raw.items():
        name = _name(raw_name, f"[targets.{raw_name}]")
        if not isinstance(body, dict):
            raise ValueError(f"[targets.{name}]: expected a table")
        unknown = set(body) - {
            "argv",
            "env",
            "retries",
            "retry_delay_seconds",
            "timeout_seconds",
        }
        if unknown:
            raise ValueError(f"[targets.{name}]: unknown fields {sorted(unknown)}")
        argv = body.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > _MAX_ARGV_ITEMS
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise ValueError(
                f"[targets.{name}].argv: expected a non-empty list of at most "
                f"{_MAX_ARGV_ITEMS} strings"
            )
        if not all(_valid_target_arg(item) for item in argv) or sum(
            len(item.encode("utf-8")) for item in argv
        ) > _MAX_ARGV_BYTES:
            raise ValueError(
                f"[targets.{name}].argv: arguments must be bounded NFC text without credentials"
            )
        env = body.get("env", [])
        if (
            not isinstance(env, list)
            or len(env) > _MAX_ARGV_ITEMS
            or not all(isinstance(item, str) and _ENV_NAME.fullmatch(item) for item in env)
            or len(set(env)) != len(env)
        ):
            raise ValueError(
                f"[targets.{name}].env: expected unique environment variable names"
            )
        retries = body.get("retries", 2)
        if type(retries) is not int or not 0 <= retries <= _MAX_RETRIES:
            raise ValueError(f"[targets.{name}].retries: integer from 0 to {_MAX_RETRIES}")
        retry_delay = _finite_number(
            body.get("retry_delay_seconds", 0.0),
            f"[targets.{name}].retry_delay_seconds",
            minimum=0.0,
            maximum=_MAX_RETRY_DELAY_SECONDS,
        )
        timeout = _finite_number(
            body.get("timeout_seconds", 60.0),
            f"[targets.{name}].timeout_seconds",
            minimum=0.001,
            maximum=_MAX_TIMEOUT_SECONDS,
        )
        targets[name] = EventTargetCfg(
            argv=tuple(argv),
            env=tuple(env),
            retries=retries,
            retry_delay_seconds=retry_delay,
            timeout_seconds=timeout,
        )
    return targets


def _webhook(body, routes_raw) -> WebhookCfg | None:
    if body is None:
        return None
    body = _table(body, "[channels.webhook]")
    unknown = set(body) - {"host", "port", "token_env"}
    if unknown:
        raise ValueError(f"[channels.webhook]: unknown fields {sorted(unknown)}")
    routes: dict = {}
    routes_raw = _table(routes_raw, "[routes]")
    for name, rb in routes_raw.items():
        rb = _table(rb, f"[routes.{name}]")
        unknown = set(rb) - {"command"}
        if unknown:
            raise ValueError(f"[routes.{name}]: unknown fields {sorted(unknown)}")
        if not rb.get("command"):
            raise ValueError(f"[routes.{name}]: command is required")
        routes[name] = rb["command"]
    return WebhookCfg(host=str(body.get("host", "127.0.0.1")), port=int(body.get("port", 8787)),
                      token_env=str(body.get("token_env", "GATEWAY_TOKEN")), routes=routes)


def _telegram(body, projects_raw, targets: dict[str, EventTargetCfg]) -> TelegramCfg | None:
    if body is None:
        return None
    body = _table(body, "[channels.telegram]")
    unknown = set(body) - {"rig", "default", "token_env", "chat_id_env"}
    if unknown:
        raise ValueError(f"[channels.telegram]: unknown fields {sorted(unknown)}")

    # ``rig`` and ``[projects.*]`` are required only by watch ingress, which
    # spawns a session target. Outbound delivery needs only the bot token and
    # chat. Fail-closed validation still happens in ``cmd_watch``: spawning
    # without a configured registry remains impossible.
    rig_raw = body.get("rig", "")
    if not isinstance(rig_raw, str):
        raise ValueError("[channels.telegram].rig: expected a string")
    rig = os.path.expanduser(rig_raw)

    projects: dict = {}
    projects_raw = _table(projects_raw, "[projects]")
    for name, pb in projects_raw.items():
        pb = _table(pb, f"[projects.{name}]")
        unknown = set(pb) - {"path", "args", "target"}
        if unknown:
            raise ValueError(f"[projects.{name}]: unknown fields {sorted(unknown)}")
        project_name = _name(name, f"[projects.{name}]")
        target = pb.get("target", "rig")
        target = _name(target, f"[projects.{name}].target")
        if target != "rig" and target not in targets:
            raise ValueError(f"[projects.{name}].target: {target!r} is not declared in [targets]")
        path_raw = pb.get("path", "")
        if not isinstance(path_raw, str):
            raise ValueError(f"[projects.{name}]: path must be a string")
        if target == "rig" and not path_raw:
            raise ValueError(f"[projects.{name}]: path is required for target=rig")
        if target != "rig" and path_raw:
            raise ValueError(f"[projects.{name}]: path is allowed only for target=rig")
        p = os.path.expanduser(path_raw)
        args = pb.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError(f"[projects.{name}]: args must be a list of strings")
        if target != "rig" and args:
            raise ValueError(f"[projects.{name}]: args are allowed only for target=rig")
        projects[project_name] = Project(path=p, args=list(args), target=target)
    if any(project.target == "rig" for project in projects.values()) and not rig:
        raise ValueError("project target=rig requires [channels.telegram].rig")

    default_raw = body.get("default", "")
    if not isinstance(default_raw, str):
        raise ValueError("[channels.telegram].default: expected a string")
    default = default_raw
    if default and default not in projects:
        raise ValueError(f"[channels.telegram].default: {default!r} is not declared in [projects]")
    if not default and len(projects) == 1:
        default = next(iter(projects))             # One project becomes the implicit default.

    token_env = body.get("token_env", "TELEGRAM_BOT_TOKEN")
    chat_id_env = body.get("chat_id_env", "TELEGRAM_CHAT_ID")
    if not isinstance(token_env, str) or not isinstance(chat_id_env, str):
        raise ValueError("[channels.telegram]: token_env and chat_id_env must be strings")
    return TelegramCfg(rig=rig, default=default, projects=projects,
                       token_env=token_env, chat_id_env=chat_id_env)


def _linear(body) -> LinearCfg | None:
    if body is None:
        return None
    body = _table(body, "[channels.linear]")
    unknown = set(body) - {"token_env", "workspace"}
    if unknown:
        raise ValueError(f"[channels.linear]: unknown fields {sorted(unknown)}")
    return LinearCfg(token_env=str(body.get("token_env", "LINEAR_API_KEY")),
                     workspace=str(body.get("workspace", "")))


def resolve_webhook_token(cfg: WebhookCfg, env: dict | None = None) -> str:
    """Resolve the webhook token from the environment before binding."""
    src = os.environ if env is None else env
    token = str(src.get(cfg.token_env, "") or "")
    if len(token) < MIN_TOKEN:
        raise ValueError(f"env {cfg.token_env} is required and must contain at least "
                         f"{MIN_TOKEN} characters; refusing unauthenticated startup")
    return token


def load_env(path: str | Path) -> int:
    """Merge ``<home>/.env`` into ``os.environ`` without overriding process env.

    Service credentials belong in the external gateway home, but explicitly
    exported variables take precedence over file values. A missing or unreadable
    file returns zero instead of failing because credentials may already exist
    in the environment.
    """
    n = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
                    n += 1
    except OSError:
        return 0
    return n
