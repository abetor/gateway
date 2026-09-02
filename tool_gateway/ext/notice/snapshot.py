"""Collect tool and scheduler status JSON into a status envelope.

Dependency direction permits the gateway to invoke tools, so snapshot assembly
lives here rather than in a scheduler. Each shell command in
``[[snapshot.sources]]`` returns JSON with a ``snapshot`` object containing a
flow, workers, and queue. Optional scheduler data contributes process IDs,
groups, start times, stalls, and wait deadlines. Workers and jobs are correlated
by process group. An idle snapshot stays silent but records that decision and
returns exit 0.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

from ...core import EXIT_FAIL, EXIT_OK
from ...core.config import Config, SnapshotSource
from ...core.log import bind
from .envelope import Envelope

MAX_SOURCE_OUTPUT = 4 * 1024 * 1024
MAX_WORKERS_PER_FLOW = 16
MAX_QUEUE_SHOWN = 8


@dataclass
class Worker:
    flow: str
    item: str
    name: str = ""
    pid: int | None = None
    job: str = ""
    stage_i: int | None = None
    stage_n: int | None = None
    stage_name: str = ""
    done: int | None = None
    total: int | None = None
    started_at: str = ""
    eta_seconds: float | None = None
    eta_total_seconds: float | None = None
    state: str = "running"
    note: str = ""
    metrics: list[str] = field(default_factory=list)
    # Fields attached from scheduler metadata.
    job_state: str = ""             # stall 90m / waiting until 12:45
    job_since: str = ""


@dataclass
class Flow:
    name: str
    workers: list[Worker] = field(default_factory=list)
    queue: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _int(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _num(value) -> float | None:
    return float(value) if type(value) in {int, float} and value >= 0 else None


def _text(value, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_fragment(flow_default: str, payload: object) -> Flow:
    """Convert tool JSON to a flow, degrading malformed fields to errors."""
    flow = Flow(name=flow_default)
    if not isinstance(payload, dict):
        flow.errors.append("not a JSON object")
        return flow
    fragment = payload.get("snapshot")
    if not isinstance(fragment, dict):
        flow.errors.append("missing snapshot key")
        return flow
    name = fragment.get("flow")
    if isinstance(name, str) and name.strip():
        flow.name = _text(name, 32)
    for raw in (fragment.get("workers") or [])[:MAX_WORKERS_PER_FLOW]:
        if not isinstance(raw, dict) or not isinstance(raw.get("item"), str):
            continue
        stage = raw.get("stage") if isinstance(raw.get("stage"), dict) else {}
        metrics = raw.get("metrics") if isinstance(raw.get("metrics"), list) else []
        flow.workers.append(Worker(
            flow=flow.name, item=_text(raw["item"], 48), name=_text(raw.get("name"), 120),
            pid=_int(raw.get("pid")), job=_text(raw.get("job"), 64),
            stage_i=_int(stage.get("i")), stage_n=_int(stage.get("n")),
            stage_name=_text(stage.get("name"), 24),
            done=_int(raw.get("done")), total=_int(raw.get("total")),
            started_at=_text(raw.get("started_at"), 40),
            eta_seconds=_num(raw.get("eta_seconds")),
            eta_total_seconds=_num(raw.get("eta_total_seconds")),
            state=_text(raw.get("state"), 24) or "running",
            note=_text(raw.get("note"), 200),
            metrics=[_text(item, 60) for item in metrics if _text(item)][:6],
        ))
    for raw in (fragment.get("queue") or [])[:200]:
        if isinstance(raw, dict) and isinstance(raw.get("item"), str):
            flow.queue.append({"item": _text(raw["item"], 48), "name": _text(raw.get("name"), 120),
                               "state": _text(raw.get("state"), 16) or "waiting"})
    return flow


def _run(command: str, timeout: float, runner: Callable) -> tuple[object, str]:
    """Return ``(json | None, error)`` using an injectable subprocess runner."""
    try:
        result = runner(["/bin/sh", "-c", command], capture_output=True, text=True,
                        errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, f"timeout after {timeout:g}s"
    except OSError as error:
        return None, str(error)
    out = result.stdout or ""
    if len(out) > MAX_SOURCE_OUTPUT:
        return None, "output exceeds limit"
    try:
        return json.loads(out), ""
    except (ValueError, RecursionError):
        tail = (result.stderr or out).strip().splitlines()
        return None, f"not JSON (exit {result.returncode}): {tail[-1][:120] if tail else ''}"


def _pgid(pid: int | None) -> int | None:
    if pid is None:
        return None
    try:
        return os.getpgid(pid)
    except (OSError, OverflowError):
        return None


def _jobs(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        return []
    rows = []
    for job in payload["jobs"]:
        if not isinstance(job, dict) or not isinstance(job.get("name"), str):
            continue
        state = job.get("state") if isinstance(job.get("state"), dict) else {}
        runtime = state.get("runtime") if isinstance(state.get("runtime"), dict) else {}
        rows.append({
            "name": job["name"], "kind": _text(runtime.get("kind"), 16),
            "pgid": _int(runtime.get("pgid")), "pid": _int(runtime.get("pid")),
            "since": _text(runtime.get("since"), 40), "until": _text(runtime.get("until"), 40),
            "stall_minutes": _int(runtime.get("stall_minutes")),
        })
    return rows


def _hhmm(value: str, now: datetime.datetime) -> str:
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    local = parsed.astimezone(now.tzinfo)
    if local.date() != now.date():
        return local.strftime("%d.%m %H:%M")
    return local.strftime("%H:%M")


def _elapsed(started_at: str, now: datetime.datetime) -> float | None:
    try:
        parsed = datetime.datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (now - parsed).total_seconds())


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    total = int(seconds // 60)
    days, rest = divmod(total, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h"
    return f"{hours}h{minutes:02d}"


def _attach_jobs(flows: list[Flow], jobs: list[dict], now: datetime.datetime,
                 pgid_of: Callable[[int | None], int | None]) -> list[dict]:
    """Attach scheduler jobs by process group and return unmatched active jobs."""
    by_pgid = {job["pgid"]: job for job in jobs if job["pgid"] is not None}
    matched: set[str] = set()
    for flow in flows:
        for worker in flow.workers:
            job = by_pgid.get(pgid_of(worker.pid)) if worker.pid is not None else None
            if job is None and worker.job:
                job = next((item for item in jobs if item["name"] == worker.job), None)
            if job is None:
                continue
            matched.add(job["name"])
            worker.job = worker.job or job["name"]
            worker.job_since = job["since"]
            if job["kind"] == "stall":
                worker.job_state = f"stall {job['stall_minutes'] or '?'}m"
            elif job["kind"] == "waiting" and job["until"]:
                worker.job_state = f"waiting until {_hhmm(job['until'], now)}"
    return [job for job in jobs
            if job["name"] not in matched and job["kind"] in {"running", "stall"}]


def render_worker(worker: Worker, index: int, now: datetime.datetime) -> list[str]:
    lines = [f"w{index} {worker.item}"]
    if worker.name and worker.name != worker.item:
        lines.append(f"  {worker.name}")
    parts = []
    stage = ""
    if worker.stage_i is not None and worker.stage_n:
        stage = f"{worker.stage_i}/{worker.stage_n} "
    stage += worker.stage_name
    if worker.done is not None:
        stage = (stage + " " if stage else "") + f"{worker.done}/{worker.total if worker.total is not None else '?'}"
    if stage.strip():
        parts.append(stage.strip())
    elapsed = _elapsed(worker.started_at, now) if worker.started_at else None
    if elapsed is None and worker.job_since:
        elapsed = _elapsed(worker.job_since, now)
    if elapsed is not None or worker.eta_total_seconds is not None:
        total = (elapsed or 0.0) + worker.eta_total_seconds if worker.eta_total_seconds is not None else None
        parts.append(f"{fmt_duration(elapsed)}/{'~' + fmt_duration(total) if total is not None else '?'}")
    eta = worker.eta_total_seconds if worker.eta_total_seconds is not None else worker.eta_seconds
    if eta is not None:
        end = now + datetime.timedelta(seconds=eta)
        parts.append(f"~{end.strftime('%H:%M') if end.date() == now.date() else end.strftime('%d.%m %H:%M')}")
    parts.append(worker.job_state or worker.state)
    lines.append("  " + " · ".join(parts))
    if worker.note:
        lines.append(f"  {worker.note}")
    if worker.metrics:
        lines.append("  " + " · ".join(worker.metrics))
    return lines


def render(flows: list[Flow], loose_jobs: list[dict], now: datetime.datetime) -> tuple[str, list[str]]:
    """Render the status envelope's title and lines."""
    total_workers = sum(len(flow.workers) for flow in flows) + len(loose_jobs)
    lines: list[str] = []
    for flow in flows:
        if not flow.workers and not flow.queue and not flow.errors:
            continue
        waiting = [item for item in flow.queue if item["state"] == "waiting"]
        head = f"[{flow.name}] {len(flow.workers)} workers"
        if flow.queue:
            head += f" · queue {len(waiting)}"
        lines.append(head)
        for index, worker in enumerate(flow.workers, 1):
            lines.extend(render_worker(worker, index, now))
        if waiting:
            shown = ", ".join(item["item"] for item in waiting[:MAX_QUEUE_SHOWN])
            if len(waiting) > MAX_QUEUE_SHOWN:
                shown += f", +{len(waiting) - MAX_QUEUE_SHOWN}"
            lines.append(f"queue: {shown}")
        for error in flow.errors:
            lines.append(f"? {error}")
        lines.append("")
    if loose_jobs:
        lines.append(f"[scheduler] running {len(loose_jobs)}")
        for job in loose_jobs:
            state = f"stall {job['stall_minutes'] or '?'}m" if job["kind"] == "stall" else "running"
            lines.append(f"{job['name']} · {state} {fmt_duration(_elapsed(job['since'], now))}")
        lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    title = f"{now.strftime('%H:%M')} · {total_workers} workers"
    return title, lines


def collect(cfg: Config, *, runner: Callable = subprocess.run, now: Optional[datetime.datetime] = None,
            pgid_of: Callable[[int | None], int | None] = _pgid) -> tuple[list[Flow], list[dict], datetime.datetime]:
    now = now or datetime.datetime.now().astimezone()
    if cfg.snapshot is None:
        raise ValueError("[snapshot] is not configured in gateway.toml")
    flows: dict[str, Flow] = {}
    for source in cfg.snapshot.sources:
        payload, error = _run(source.command, cfg.snapshot.timeout_seconds, runner)
        fragment = parse_fragment(source.flow, payload) if payload is not None else Flow(source.flow, errors=[error])
        flow = flows.setdefault(fragment.name, Flow(fragment.name))
        flow.workers.extend(fragment.workers)
        seen = {item["item"] for item in flow.queue}
        flow.queue.extend(item for item in fragment.queue if item["item"] not in seen)
        flow.errors.extend(f"{source.flow}: {err}" for err in fragment.errors)
    jobs: list[dict] = []
    if cfg.snapshot.scheduler_command:
        payload, error = _run(cfg.snapshot.scheduler_command, cfg.snapshot.timeout_seconds, runner)
        if payload is None:
            flows.setdefault("scheduler", Flow("scheduler")).errors.append(f"scheduler: {error}")
        else:
            jobs = _jobs(payload)
    ordered = [flows[name] for name in sorted(flows, key=lambda key: (key == "scheduler", key))]
    loose = _attach_jobs(ordered, jobs, now, pgid_of)
    return ordered, loose, now


def snapshot(home, cfg: Config, *, channel_name: str = "telegram", print_only: bool = False,
             runner: Callable = subprocess.run, http=None, events: Optional[Callable] = None,
             now: Optional[datetime.datetime] = None,
             pgid_of: Callable[[int | None], int | None] = _pgid) -> int:
    emit = events or bind(home)
    try:
        flows, loose, moment = collect(cfg, runner=runner, now=now, pgid_of=pgid_of)
    except ValueError as error:
        print(f"snapshot: {error}", file=sys.stderr)
        return EXIT_FAIL
    title, lines = render(flows, loose, moment)
    busy = any(flow.workers for flow in flows) or loose
    errors = [err for flow in flows for err in flow.errors]
    if not busy and not errors:
        emit("delivery", channel=channel_name, source="deliver", text="", kind="status",
             sent=False, reason="idle")
        print("snapshot: idle; no notification sent")
        return EXIT_OK
    env = Envelope(source="status", about="", cls="status", title=title, lines=lines,
                   at=moment.isoformat(timespec="seconds"))
    if print_only:
        from .render import render as render_text
        print(render_text(env, home=home))
        return EXIT_OK
    from ..deliver.deliver import deliver_envelope
    return deliver_envelope(home, cfg, env, channel_name=channel_name, http=http, events=events)
