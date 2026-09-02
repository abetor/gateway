"""Harness port for LLM work through interchangeable agent CLIs.

The CLI flag behavior was verified in live runs before this locally owned code
was copied here. Do not change flags or patterns without another live check.

The minimum harness contract is a headless one-shot process with a prompt and
cwd, a textual final response on stdout, and distinguishable failure. Other
features are explicit capabilities. The wrapper emulates what a harness lacks
instead of claiming unsupported behavior. This module also classifies stops as
done, quota, transient, or fatal because no machine-readable subscription quota
endpoint exists; classification must use output patterns.
"""
from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

# Patterns shared by CLI harnesses; adapters add their own through quota_patterns.
#
# CLASS BOUNDARY: an earlier production defect grouped 429 and "rate limit"
# with real subscription exhaustion. A concurrency burst produced ordinary 429
# responses, the classifier returned quota, the CLI exited 75, and an external
# driver removed remaining work even though waiting seconds would have fixed it.
# Classes must be separated by their remedy, not by a shared word.
#
# _QUOTA means a subscription budget is exhausted and requires waiting for a
# reset window measured in hours or days. Match budget language, not request rate.
# Observed forms include:
# claude - "Claude usage limit reached ... resets at", "You've hit your weekly limit",
# "5-hour limit reached"; codex - "You've hit your usage limit", "out of credits".
# The negative lookbehind is the boundary: "usage limit exceeded" is quota,
# while the common throttling phrase "rate limit exceeded" is not.
_QUOTA = re.compile(r"usage limit|weekly limit|session limit|5-hour|out of credits|quota"
                    r"|(?:hit|reached) your [^.\n]{0,24}limit"
                    r"|(?<!rate[ -])\blimit (?:reached|exceeded)", re.I)
# _TRANSIENT is repaired by retrying after seconds or minutes. It includes burst
# throttling such as 429, "too many requests", and "rate limit". Both harnesses
# treat this separately from quota and already retry with backoff, so when it
# reaches this layer, waiting again is preferable to a quota checkpoint.
# For mixed text such as "429: usage limit reached", classify checks quota first.
# This is deliberate: hitting a real quota again is worse than an extra wait,
# and budget language is more specific than the general 429 status.
_TRANSIENT = re.compile(r"\b(?:429|500|502|503|529)\b|too many requests|rate.?limit"
                        r"|overloaded|timed?.?out|connection|"
                        r"temporarily|try again|ECONNRESET|EAI_AGAIN", re.I)

# Map stop classes to the shared CLI exit-code contract.
STOP_TO_EXIT = {"done": 0, "quota": 75, "transient": 111, "fatal": 1}


@dataclass
class Capabilities:
    """Capabilities supported natively; the wrapper emulates the rest."""
    json_events: bool
    schema_output: bool
    native_resume: bool
    subagents: bool
    mcp: bool


@dataclass
class RunResult:
    ok: bool
    text: str
    exit_code: int
    session_id: Optional[str] = None
    stop: str = "done"                 # done | quota | transient | fatal
    cost_usd: Optional[float] = None
    raw: dict = field(default_factory=dict)
    stderr: str = ""


class HarnessAdapter(ABC):
    """Per-CLI command construction and output parsing; ``run`` remains shared."""
    name: str = "harness"

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def build_cmd(self, prompt: str, *, model: Optional[str] = None,
                  effort: Optional[str] = None, resume_session_id: Optional[str] = None,
                  schema_path: Optional[str] = None, system_prompt_path: Optional[str] = None,
                  allowed_tools: Optional[str] = None) -> list[str]: ...

    @abstractmethod
    def parse_output(self, stdout: str, exit_code: int) -> RunResult: ...

    def quota_patterns(self) -> Optional[re.Pattern]:
        """Return per-CLI quota patterns in addition to the shared patterns."""
        return None

    def classify(self, result: RunResult) -> str:
        """Classify a result as done, quota, transient, or fatal.

        Check order is part of the contract:

        1. Quota is checked even for successful exit because a harness can exit
           0 while saying it hit a limit. Quota also wins mixed text such as
           ``429: usage limit reached``.
        2. A successful result is done. Throttling is deliberately checked
           afterward: 429 text with exit 0 means the harness retried and
           succeeded, and externally collected content about rate limits must
           not turn a successful run transient.
        3. Transient patterns cover burst throttling and network failures only
           on a failed call.

        Raw keys form part of the port contract: ``subtype`` and
        ``api_error_status`` for single-JSON output, and ``error`` for JSONL
        error events.
        """
        blob = f"{result.text}\n{result.stderr}\n{result.raw.get('subtype', '')}\n" \
               f"{result.raw.get('api_error_status', '')}\n{result.raw.get('error', '')}"
        extra = self.quota_patterns()
        if _QUOTA.search(blob) or (extra and extra.search(blob)):
            return "quota"
        if result.ok:
            return "done"
        if _TRANSIENT.search(blob):
            return "transient"
        return "fatal"

    def run(self, prompt: str, *, cwd: str, timeout: Optional[float] = None,
            **build_kw) -> RunResult:
        """Run one harness call; this is the only process-spawning location."""
        cmd = self.build_cmd(prompt, **build_kw)
        try:
            # DEVNULL prevents Codex from reading inherited stdin until EOF and hanging.
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or "") if isinstance(e.stdout, str) else ""
            return RunResult(ok=False, text=out, exit_code=-1,
                             stderr="wall-timeout", stop="transient")
        res = self.parse_output(proc.stdout, proc.returncode)
        res.stderr = proc.stderr or ""
        res.stop = self.classify(res)
        if res.stop != "done":
            res.ok = False
        return res
