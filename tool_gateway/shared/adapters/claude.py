"""Claude Code CLI adapter whose flag behavior was verified in live runs.

Do not change command-line mechanics without another live check.

`claude -p --output-format json [...] -- "prompt"` -> stdout single-json:
{result, session_id, total_cost_usd, is_error, subtype, ...}.

Verified behavior:

- The schema is passed inline to ``--json-schema``, not as a path.
- ``--`` before the prompt is required because ``--allowedTools`` greedily
  consumes the next positional argument.
- Resume uses ``--resume <session_id>``.
- ``claude -p`` normally uses the subscription quota, but a reported billing
  issue can charge API usage; callers should log usage and compare expectations.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .base import Capabilities, HarnessAdapter, RunResult

# The default allow-list permits web and reading but no project writes, providing
# a technical read-only boundary. A writing caller must pass its own tool list.
READ_ONLY_TOOLS = "WebSearch WebFetch Read Grep Glob LS"


class ClaudeAdapter(HarnessAdapter):
    name = "claude"

    def __init__(self, binary: str = "claude"):
        self.binary = binary

    def capabilities(self) -> Capabilities:
        return Capabilities(json_events=True, schema_output=True, native_resume=True,
                            subagents=True, mcp=True)

    def build_cmd(self, prompt, *, model=None, effort=None, resume_session_id=None,
                  schema_path=None, system_prompt_path=None, allowed_tools=None):
        cmd = [self.binary, "-p", "--output-format", "json",
               "--permission-mode", "default"]
        if model:
            cmd += ["--model", model]
        # In non-interactive print mode, default permissions cannot ask for a
        # tool outside this list, so the list is the call's capability boundary.
        cmd += ["--allowedTools", allowed_tools or READ_ONLY_TOOLS]
        if effort:
            cmd += ["--effort", effort]
        if system_prompt_path:
            # Add a separate system prompt without replacing project instructions.
            cmd += ["--append-system-prompt-file", system_prompt_path]
        if resume_session_id:
            cmd += ["--resume", resume_session_id]
        if schema_path:
            schema = Path(schema_path).read_text("utf-8").strip()
            if not schema:
                raise ValueError(f"empty schema {schema_path}; refusing an unconstrained call")
            cmd += ["--json-schema", schema]  # Claude expects inline schema content.
        cmd += ["--", prompt]
        return cmd

    def parse_output(self, stdout, exit_code) -> RunResult:
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            # Preserve non-JSON output from a crash before format initialization.
            return RunResult(ok=exit_code == 0 and bool(stdout.strip()),
                             text=stdout, exit_code=exit_code)
        ok = exit_code == 0 and not data.get("is_error", False)
        return RunResult(ok=ok, text=data.get("result") or "", exit_code=exit_code,
                         session_id=data.get("session_id"),
                         cost_usd=data.get("total_cost_usd"), raw=data)

    def quota_patterns(self) -> re.Pattern:
        # Observed subscription and API limit markers in JSON response subtypes.
        return re.compile(r"error_max_turns|resets at|upgrade to|claude usage", re.I)
