"""CLI for local Telegram, webhook, and machine-target ingress and delivery.

Subcommands include read-only diagnostics and route discovery, webhook serving,
Telegram polling, notification delivery, versioned machine transport, and
capability discovery.

``--home`` is required and never defaults to cwd. Configuration, credentials,
logs, and state belong in an explicitly selected external data directory, not
inside the repository.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

# The 0/75/111/1 exit contract lives in the core so ``ext`` does not depend on
# the CLI layer. Re-export it here to preserve ``tool_gateway.cli.EXIT_*``.
from .core import EXIT_FAIL, EXIT_OK, EXIT_QUOTA, EXIT_TRANSIENT  # noqa: F401
from .shared.cliargs import ArgumentParser   # argparse usage errors exit with 1, not 2.

# Default secret environment names. ``doctor`` reports only boolean presence
# and never prints values. A live config may override these names.
SECRET_HINTS: tuple[str, ...] = ("GATEWAY_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
MACHINE_SCHEMA_VERSION = 1
MAX_MACHINE_OUTPUT_BYTES = 64 * 1024


def _project_version() -> str:
    """Read the version from a checkout or its installed package metadata."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        value = tomllib.loads(pyproject.read_text("utf-8"))["project"]["version"]
        if isinstance(value, str) and value:
            return value
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        pass
    try:
        return package_version("tool-gateway")
    except PackageNotFoundError as error:
        raise RuntimeError("could not determine the tool-gateway version") from error


TOOL_VERSION = _project_version()

# Channel registration point. The core does not import ``ext``; the CLI does.
from .core import log as corelog                       # noqa: E402
from .core.channel import make_channel                 # noqa: E402
from .core.config import (DEFAULT_CLASSES, load_config, load_env,  # noqa: E402
                          resolve_webhook_token)
from .ext.telegram import channel as _telegram         # noqa: E402,F401 - register("telegram")


def main(argv: list[str] | None = None) -> int:
    p = ArgumentParser(
        prog="tool-gateway",
        description="local ingress and delivery through Telegram and webhooks")
    p.add_argument("--home",
                   help="gateway data directory containing gateway.toml, .env, log/, and "
                        "state/; it must be outside the repository and has no default")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="inspect the environment, config, and channels without mutation")
    sub.add_parser("routes", help="show webhook routes and Telegram projects")
    sub.add_parser("serve", help="run webhook HTTP ingress")
    w = sub.add_parser("watch", help="run the Telegram poll -> dispatch -> send loop")
    w.add_argument("--interval", type=float, default=2.0, help="seconds between polls")
    w.add_argument("--once", action="store_true", help="run one inline dispatch pass for testing")
    d = sub.add_parser("deliver", help="deliver a v2 envelope, legacy structured notice, or plain text")
    d.add_argument("text", nargs="?", default="", help="plain delivery text")
    d.add_argument("--stdin", action="store_true",
                   help="read text from stdin; with structured fields, the first line is the title")
    d.add_argument("--json", action="store_true",
                   help="read a v2 envelope JSON object from stdin")
    d.add_argument("--from", dest="source_v2", default="",
                   help="v2 envelope: the sending flow or tool")
    d.add_argument("--about", default="", help="v2 envelope: unique operation name")
    d.add_argument("--class", dest="cls", default="",
                   help="v2 envelope: registered class (status/event/alert/action/content/ack)")
    d.add_argument("--line", action="append", default=[], help="v2 envelope: repeatable fact line")
    d.add_argument("--name", default="", help="v2 envelope: human-readable subject name")
    d.add_argument("--action", default="", help="v2 envelope: one requested human action")
    d.add_argument("--urgency", choices=("low", "normal", "high"), default="normal",
                   help="v2 envelope: high bypasses availability windows")
    d.add_argument("--dedupe", default="", help="v2 envelope: explicit deduplication key")
    d.add_argument("--live", action="store_true",
                   help="v2 envelope: update one live message per (from, about)")
    d.add_argument("--kind", choices=("progress", "done", "fail", "stall", "wait", "info"),
                   help="legacy contract kind mapped to a v2 class")
    d.add_argument("--source", default="", help="legacy source, equivalent to from")
    d.add_argument("--subject", default="", help="legacy subject, equivalent to about")
    d.add_argument("--title", default="", help="title for either structured contract")
    d.add_argument("--lines", default="", help="newline-separated lines for either contract")
    d.add_argument("--channel", default="telegram", help="delivery channel; currently Telegram")
    dr = sub.add_parser("drain", help="send deferred notices and retry transient failures")
    dr.add_argument("--channel", default="telegram")
    sn = sub.add_parser("snapshot", help="collect tool status JSON into a status envelope")
    sn.add_argument("--print", action="store_true", help="print without sending")
    sn.add_argument("--channel", default="telegram")
    capabilities = sub.add_parser("capabilities", help="machine-readable CLI capabilities")
    capabilities.add_argument("--json", action="store_true", required=True)
    event = sub.add_parser("dispatch-event", help="deliver a normalized event through an argv target")
    event.add_argument("--json", action="store_true", required=True)
    receipt = sub.add_parser("receipt", help="read a receipt by event_id")
    receipt.add_argument("event_id")
    receipt.add_argument("--json", action="store_true", required=True)

    args = p.parse_args(argv)
    if args.cmd == "capabilities":
        return _emit_machine(_capabilities(), operation="capabilities")
    if args.home is None:
        p.error("the following arguments are required: --home")
    home = Path(args.home).expanduser()
    load_env(home / ".env")                        # Process env overrides external-home values.

    if args.cmd == "dispatch-event":
        return cmd_dispatch_event(home)
    if args.cmd == "receipt":
        return cmd_receipt(home, args.event_id)
    if args.cmd == "doctor":
        return doctor(home)
    if args.cmd == "routes":
        return cmd_routes(home)
    if args.cmd == "serve":
        return cmd_serve(home)
    if args.cmd == "watch":
        return cmd_watch(home, interval=args.interval, once=args.once)
    if args.cmd == "drain":
        return cmd_drain(home, channel_name=args.channel)
    if args.cmd == "snapshot":
        return cmd_snapshot(home, channel_name=args.channel, print_only=args.print)
    if args.cmd == "deliver":
        v2 = bool(args.source_v2 or args.about or args.cls or args.line or args.name
                  or args.action or args.dedupe or args.live or args.json)
        structured = bool(args.kind is not None or args.source or args.subject
                          or args.title or args.lines)
        if v2 and (args.source or args.subject):
            p.error("deliver: a v2 envelope (--from/--about/--class) cannot be mixed with "
                    "legacy --source/--subject; --kind may still hint at class/live")
        if args.json and (v2 and (args.source_v2 or args.about or args.cls or args.line)):
            p.error("deliver --json: read the complete envelope from stdin without field flags")
        if args.stdin and args.text:
            p.error("deliver --stdin: positional text cannot be mixed with stdin")
        if (structured or v2) and args.text:
            p.error("deliver: plain text cannot be mixed with structured fields")
        if args.stdin and (args.title or args.lines or args.line):
            p.error("deliver --stdin: title and lines are read from stdin")
        if args.json:
            return cmd_deliver_json(home, sys.stdin.buffer.read(), channel_name=args.channel)
        text, title, lines = args.text, args.title, args.lines
        if args.stdin:
            stdin_text = sys.stdin.read()
            if structured or v2:
                parts = stdin_text.splitlines()
                title = parts[0] if parts else ""
                lines = "\n".join(parts[1:])
            else:
                text = stdin_text
        if v2:
            # During migration, legacy hooks may know only KIND. Derive an empty
            # class from ``kind`` and treat progress as live, allowing one hook
            # command to serve both old and new callers.
            from .ext.notice.envelope import LEGACY_KIND_CLASS
            cls = args.cls or (LEGACY_KIND_CLASS.get(args.kind, "") if args.kind else "")
            live = args.live or args.kind == "progress"
            return cmd_deliver_v2(
                home, channel_name=args.channel, source=args.source_v2, about=args.about,
                cls=cls, title=title, lines=list(args.line) or lines, name=args.name,
                action=args.action, urgency=args.urgency, dedupe=args.dedupe, live=live,
            )
        return cmd_deliver(home, text, channel_name=args.channel,
                           kind=args.kind or "info", source=args.source,
                           subject=args.subject, title=title, lines=lines,
                           structured=structured)
    return EXIT_FAIL  # Unreachable with required=True; guards branch drift.


def _capability(
    name: str,
    argv: list[str],
    required_fields: list[str],
    exits: dict[str, str],
    idempotency: str,
) -> dict[str, object]:
    return {
        "name": name,
        "argv": argv,
        "machine_output": {
            "format": "json",
            "schema_version": MACHINE_SCHEMA_VERSION,
            "required_fields": required_fields,
        },
        "exit_codes": exits,
        "idempotency": idempotency,
    }


def _capabilities() -> dict[str, object]:
    return {
        "schema_version": MACHINE_SCHEMA_VERSION,
        "tool": "gateway",
        "version": TOOL_VERSION,
        "capabilities": [
            _capability(
                "dispatch-event",
                ["tool-gateway", "--home", "<home>", "dispatch-event", "--json"],
                ["schema_version", "event_id", "status", "receipt"],
                {"0": "success", "1": "failure", "111": "transient"},
                "deduplicated",
            ),
            _capability(
                "delivery-receipt",
                ["tool-gateway", "--home", "<home>", "receipt", "<event-id>", "--json"],
                ["schema_version", "event_id", "status", "dedupe_key"],
                {"0": "success", "1": "failure"},
                "read-only",
            ),
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
                    "classes": sorted(DEFAULT_CLASSES),
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
            _capability(
                "drain",
                ["tool-gateway", "--home", "<home>", "drain"],
                [],
                {"0": "success", "1": "failure"},
                "idempotent",
            ),
            _capability(
                "snapshot",
                ["tool-gateway", "--home", "<home>", "snapshot", "[--print]"],
                [],
                {"0": "success-or-idle", "1": "failure"},
                "idempotent",
            ),
        ],
    }


def _json_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _emit_machine(payload: dict[str, object], *, operation: str) -> int:
    try:
        rendered = _json_bytes(payload)
    except (TypeError, ValueError, RecursionError):
        rendered = _json_bytes({
            "schema_version": MACHINE_SCHEMA_VERSION,
            "status": "error",
            "error": {"code": "serialization_error"},
        })
        print(f"{operation}: serialization_error", file=sys.stderr)
        sys.stdout.buffer.write(rendered)
        return EXIT_FAIL
    if len(rendered) > MAX_MACHINE_OUTPUT_BYTES:
        rendered = _json_bytes({
            "schema_version": MACHINE_SCHEMA_VERSION,
            "status": "error",
            "error": {"code": "output_too_large"},
        })
        print(f"{operation}: output_too_large", file=sys.stderr)
        sys.stdout.buffer.write(rendered)
        return EXIT_FAIL
    sys.stdout.buffer.write(rendered)
    return EXIT_OK


def _event_error(event_id: str | None, code: str) -> dict[str, object]:
    return {
        "schema_version": MACHINE_SCHEMA_VERSION,
        "event_id": event_id,
        "status": "error",
        "receipt": None,
        "replayed": False,
        "error": {"code": code},
    }


def cmd_dispatch_event(home: Path) -> int:
    from .ext.event.dispatch import MAX_EVENT_BYTES, EventError, decode_event, dispatch_event

    raw = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
    event_id: str | None = None
    try:
        event = decode_event(raw)
        event_id = str(event["event_id"])
    except EventError as error:
        print(f"dispatch-event: {error.code}", file=sys.stderr)
        _emit_machine(_event_error(None, error.code), operation="dispatch-event")
        return EXIT_FAIL
    try:
        cfg = load_config(home)
    except (FileNotFoundError, OSError, ValueError, UnicodeError):
        print("dispatch-event: invalid_config", file=sys.stderr)
        _emit_machine(_event_error(event_id, "invalid_config"), operation="dispatch-event")
        return EXIT_FAIL
    target = cfg.targets.get(str(event["target"]))
    if target is None:
        print("dispatch-event: unknown_target", file=sys.stderr)
        _emit_machine(_event_error(event_id, "unknown_target"), operation="dispatch-event")
        return EXIT_FAIL
    try:
        result = dispatch_event(home, event, target)
    except EventError as error:
        print(f"dispatch-event: {error.code}", file=sys.stderr)
        _emit_machine(_event_error(event_id, error.code), operation="dispatch-event")
        return EXIT_FAIL
    print(f"dispatch-event: {result.diagnostic}", file=sys.stderr)
    emitted = _emit_machine(result.response, operation="dispatch-event")
    return result.exit_code if emitted == EXIT_OK else EXIT_FAIL


def cmd_receipt(home: Path, event_id: str) -> int:
    from .ext.event.dispatch import EventError, read_receipt

    try:
        receipt = read_receipt(home, event_id)
    except EventError as error:
        print(f"receipt: {error.code}", file=sys.stderr)
        _emit_machine(
            {
                "schema_version": MACHINE_SCHEMA_VERSION,
                "event_id": None,
                "status": "error",
                "dedupe_key": None,
                "effect_sha256": None,
                "attempts": 0,
                "error": {"code": error.code},
            },
            operation="receipt",
        )
        return EXIT_FAIL
    if receipt is None:
        _emit_machine(
            {
                "schema_version": MACHINE_SCHEMA_VERSION,
                "event_id": event_id,
                "status": "not_found",
                "dedupe_key": None,
                "effect_sha256": None,
                "attempts": 0,
            },
            operation="receipt",
        )
        return EXIT_FAIL
    return _emit_machine(receipt, operation="receipt")


def _load(home: Path):
    """Return ``(config, code, reason)`` with uniform, explicit failures.

    The textual reason is returned so delivery can preserve it in the audit log.
    """
    try:
        return load_config(home), EXIT_OK, ""
    except FileNotFoundError:
        reason = (f"missing {home / 'gateway.toml'} "
                  "(see the example in tool_gateway/core/config.py)")
    except (OSError, ValueError) as e:
        reason = f"config {home / 'gateway.toml'}: {e}"
    print("tool-gateway: " + reason, file=sys.stderr)
    return None, EXIT_FAIL, reason


def cmd_routes(home: Path) -> int:
    cfg, rc, _ = _load(home)
    if cfg is None:
        return rc
    if cfg.webhook:
        for name, cmd in sorted(cfg.webhook.routes.items()):
            print(f"POST /hook/{name:24} -> {cmd}")
        if not cfg.webhook.routes:
            print("webhook: no routes ([routes.<name>] in gateway.toml)")
    if cfg.telegram:
        for name, proj in sorted(cfg.telegram.projects.items()):
            mark = " (default)" if name == cfg.telegram.default else ""
            if proj.target == "rig":
                tail = f" · args: {' '.join(proj.args)}" if proj.args else ""
                destination = proj.path + tail
            else:
                destination = f"event target {proj.target}"
            print(f"telegram: {name}{mark} -> {destination}")
        if not cfg.telegram.projects:              # Avoid silent, apparently broken output.
            print("telegram: no projects; channel supports delivery only")
    if cfg.linear:
        print("linear: placeholder only; channel is not implemented")
    for name in sorted(cfg.targets):
        print(f"event target: {name}")
    return EXIT_OK


def cmd_serve(home: Path) -> int:
    cfg, rc, _ = _load(home)
    if cfg is None:
        return rc
    if cfg.webhook is None:
        print("tool-gateway serve: gateway.toml has no [channels.webhook]", file=sys.stderr)
        return EXIT_FAIL
    try:
        token = resolve_webhook_token(cfg.webhook)
    except ValueError as e:
        print(f"tool-gateway serve: {e}", file=sys.stderr)
        return EXIT_FAIL
    from .ext.webhook.server import WebhookServer
    print(f"tool-gateway serve · {cfg.webhook.host}:{cfg.webhook.port} · "
          f"routes: {len(cfg.webhook.routes)}", flush=True)
    WebhookServer(cfg.webhook, home, token, events=corelog.bind(home)).serve_forever()
    return EXIT_OK


def cmd_watch(home: Path, *, interval: float, once: bool) -> int:
    cfg, rc, _ = _load(home)
    if cfg is None:
        return rc
    if cfg.telegram is None:
        print("tool-gateway watch: gateway.toml has no [channels.telegram]", file=sys.stderr)
        return EXIT_FAIL
    if not cfg.telegram.projects:
        # Ingress fails closed: listening without a dispatch destination would
        # accept tasks into a void. Delivery-only config is valid, but watch is not.
        print("tool-gateway watch: at least one [projects.<name>] is required; "
              "the config enables delivery only", file=sys.stderr)
        return EXIT_FAIL
    rig_projects = {
        name: project
        for name, project in cfg.telegram.projects.items()
        if project.target == "rig"
    }
    if rig_projects:
        rig_main = Path(cfg.telegram.rig) / "rig" / "__main__.py"
        if not rig_main.is_file():
            print(f"tool-gateway watch: [channels.telegram].rig: {cfg.telegram.rig!r} "
                  "does not look like a rig repository (missing rig/__main__.py)", file=sys.stderr)
            return EXIT_FAIL
    for name, project in cfg.telegram.projects.items():
        if project.target == "rig" and not Path(project.path).is_dir():
            print(f"tool-gateway watch: [projects.{name}]: path must exist "
                  f"({project.path!r})", file=sys.stderr)
            return EXIT_FAIL
    try:
        channel = make_channel("telegram", state_dir=str(home / "state"),
                               token_env=cfg.telegram.token_env,
                               chat_id_env=cfg.telegram.chat_id_env)
    except ValueError as e:
        print(f"tool-gateway watch: {e}", file=sys.stderr)
        return EXIT_FAIL
    from .ext.telegram.serve import serve
    try:
        n = serve(channel, cfg.telegram, interval=interval, once=once,
                  state_dir=str(home / "state"), events=corelog.bind(home),
                  home=home, targets=cfg.targets)
        if once:
            print(f"tool-gateway watch: processed messages: {n}")
    except KeyboardInterrupt:
        print("tool-gateway watch: stopped")
    return EXIT_OK


def cmd_deliver(home: Path, text: str, *, channel_name: str, kind: str = "info",
                source: str = "", subject: str = "", title: str = "",
                lines: str = "", structured: bool = False) -> int:
    cfg, rc, reason = _load(home)
    if cfg is None:
        # A pre-config refusal is still a delivery event. Record it when home
        # exists so text is not lost silently. Never create a misspelled home;
        # if it does not exist, explain that no audit destination is available.
        if home.is_dir():
            try:
                refusal = {
                    "channel": channel_name,
                    "source": "deliver",
                    "text": title if structured else text,
                    "kind": kind,
                    "sent": False,
                    "reason": reason,
                }
                if structured:
                    refusal.update(notice_source=source, subject=subject, title=title,
                                   lines=lines.splitlines())
                corelog.append(home, "delivery", **refusal)
            except OSError as e:
                print(f"deliver: cannot record refusal ({home / 'log'}: {e})", file=sys.stderr)
        else:
            print(f"deliver: home {home} does not exist; refusal cannot be recorded and "
                  "the caller retains the only copy of the text", file=sys.stderr)
        return rc
    from .ext.deliver.deliver import deliver
    return deliver(home, cfg, text, channel_name=channel_name, kind=kind,
                   source=source, subject=subject, title=title, lines=lines,
                   structured=structured)


def _refuse_envelope(home: Path, channel_name: str, reason: str, payload: dict) -> int:
    """Record a pre-config v2 refusal with its reason and complete envelope."""
    if home.is_dir():
        try:
            corelog.append(home, "delivery", channel=channel_name, source="deliver",
                           text=str(payload.get("title", "")), kind=str(payload.get("class", "")),
                           sent=False, reason=reason, envelope=payload)
        except OSError as e:
            print(f"deliver: cannot record refusal ({home / 'log'}: {e})", file=sys.stderr)
    else:
        print(f"deliver: home {home} does not exist; refusal cannot be recorded and the caller retains the envelope",
              file=sys.stderr)
    print(f"deliver: {reason}", file=sys.stderr)
    return EXIT_FAIL


def cmd_deliver_v2(home: Path, *, channel_name: str, source: str, about: str, cls: str,
                   title: str, lines, name: str, action: str, urgency: str, dedupe: str,
                   live: bool) -> int:
    from .ext.notice.envelope import Envelope, normalize_lines

    env = Envelope(source=source, about=about, cls=cls, title=title,
                   lines=normalize_lines(lines), name=name, action=action,
                   urgency=urgency, dedupe=dedupe, live=live)
    return _deliver_envelope(home, env, channel_name=channel_name)


def cmd_deliver_json(home: Path, raw: bytes, *, channel_name: str) -> int:
    from .ext.notice.envelope import EnvelopeError, from_json

    try:
        env = from_json(raw)
    except EnvelopeError as error:
        return _refuse_envelope(home, channel_name, str(error), {"title": "", "class": ""})
    return _deliver_envelope(home, env, channel_name=channel_name)


def _deliver_envelope(home: Path, env, *, channel_name: str) -> int:
    cfg, rc, reason = _load(home)
    if cfg is None:
        _refuse_envelope(home, channel_name, reason, env.to_dict())
        return rc
    from .ext.deliver.deliver import deliver_envelope
    return deliver_envelope(home, cfg, env, channel_name=channel_name)


def cmd_drain(home: Path, *, channel_name: str) -> int:
    cfg, rc, reason = _load(home)
    if cfg is None:
        print(f"drain: {reason}", file=sys.stderr)
        return rc
    from .ext.deliver.deliver import drain
    return drain(home, cfg, channel_name=channel_name)


def cmd_snapshot(home: Path, *, channel_name: str, print_only: bool) -> int:
    cfg, rc, reason = _load(home)
    if cfg is None:
        print(f"snapshot: {reason}", file=sys.stderr)
        return rc
    from .ext.notice.snapshot import snapshot
    return snapshot(home, cfg, channel_name=channel_name, print_only=print_only)


def doctor(home: Path, http=None) -> int:
    """Run diagnostics without creating or writing anything.

    Report missing prerequisites for the operator to install. Secrets are shown
    only as boolean presence, never as values. ``http`` is injectable for a
    hermetic test of the channel's live ping.
    """
    problems: list[str] = []
    notes: list[str] = []
    tools = {name: shutil.which(name) is not None for name in ("git", "python3")}
    problems += ["missing executable " + name for name, ok in tools.items() if not ok]
    if not home.is_dir():
        problems.append("missing home directory " + str(home) + " (create it explicitly)")
    elif not os.access(home, os.W_OK):
        problems.append("home directory is not writable: " + str(home))

    cfg = None
    secret_names = list(SECRET_HINTS)
    if home.is_dir():
        try:
            cfg = load_config(home)
        except FileNotFoundError:
            problems.append("missing gateway.toml in home (see the example in "
                            "tool_gateway/core/config.py)")
        except (OSError, ValueError) as e:
            problems.append(f"gateway.toml: {e}")
    if cfg:
        secret_names = []
        if cfg.webhook:
            secret_names.append(cfg.webhook.token_env)
            if len(os.environ.get(cfg.webhook.token_env, "")) < 16:
                problems.append(f"webhook: env {cfg.webhook.token_env} is empty or shorter than 16 characters")
            notes.append(f"webhook: {cfg.webhook.host}:{cfg.webhook.port}, "
                         f"routes {len(cfg.webhook.routes)}")
        if cfg.telegram:
            secret_names += [cfg.telegram.token_env, cfg.telegram.chat_id_env]
            if cfg.telegram.projects:
                rig_count = sum(
                    project.target == "rig" for project in cfg.telegram.projects.values()
                )
                generic_count = len(cfg.telegram.projects) - rig_count
                notes.append(f"telegram: projects {len(cfg.telegram.projects)} "
                             f"(rig={rig_count}, event-target={generic_count})"
                             + ("" if cfg.telegram.default
                                else " (no default: tasks require a prefix)"))
            else:
                notes.append("telegram: delivery only; watch cannot start without [projects.*]")
            if not all(os.environ.get(n) for n in
                       (cfg.telegram.token_env, cfg.telegram.chat_id_env)):
                problems.append(f"telegram: missing env {cfg.telegram.token_env} / "
                                f"{cfg.telegram.chat_id_env} (credentials belong in <home>/.env)")
            else:                                  # A live ping exposes expired credentials here.
                live = make_channel("telegram", http=http,
                                    token_env=cfg.telegram.token_env,
                                    chat_id_env=cfg.telegram.chat_id_env).check()
                if live:
                    problems.append(f"telegram: {live}")
        if cfg.linear:
            secret_names.append(cfg.linear.token_env)
            notes.append("linear: placeholder only; channel is not implemented")
        if cfg.targets:
            notes.append("event targets: " + ", ".join(sorted(cfg.targets)))
            for name, target in sorted(cfg.targets.items()):
                missing = [env_name for env_name in target.env if not os.environ.get(env_name)]
                if missing:
                    problems.append(
                        f"event target {name}: missing env " + ", ".join(missing)
                    )

    print("tool-gateway doctor: " + ("READY" if not problems else "NOT READY"))
    print("  tools: " + ", ".join(
        n + "=" + ("present" if ok else "MISSING") for n, ok in tools.items()))
    print("  home: " + str(home) + ("" if home.is_dir() else " (missing)"))
    for note in notes:
        print("  " + note)
    if secret_names:
        print("  secrets (env presence only): " + ", ".join(
            n + "=" + ("present" if os.environ.get(n) else "missing") for n in secret_names))
    for pr in problems:
        print("  problem: " + pr)
    return EXIT_OK if not problems else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
