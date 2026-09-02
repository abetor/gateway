"""OpenAI Codex CLI adapter with command behavior verified in live runs.

Do not change command-line mechanics without another live check.

`codex exec [resume <id>] --json -c approval_policy=never [--sandbox ...]
[--model M] [-c model_reasoning_effort=E] [-c model_instructions_file=PATH]
[--output-schema PATH] "prompt"` produces a JSONL event stream
(thread.started/thread_id, item.completed/agent_message, turn.completed/usage).

Verified behavior:

- ``base.run`` uses ``stdin=DEVNULL`` because inherited stdin can hang ``codex exec``.
- ``--skip-git-repo-check`` supports external data directories that are not repos.
- Resume is the ``exec resume <id>`` subcommand and does not accept ``--sandbox``;
  sandbox mode must use a config override there.
- Approval policy and reasoning effort are config overrides, not exec flags.
- Output schema is passed by path, unlike Claude's inline schema.
- Schema and resume are incompatible.
- Subscription authentication rejects Claude-family model names, so omitting
  ``--model`` selects the account default.
- Codex reports tokens rather than monetary cost; usage stays in ``raw`` and
  ``cost_usd`` is ``None``.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import Capabilities, HarnessAdapter, RunResult

# Full and short Claude-family model aliases.
_CLAUDE_ALIASES = {"sonnet", "opus", "haiku"}


def _claude_family(model: str) -> bool:
    m = model.lower()
    return m.startswith("claude") or m in _CLAUDE_ALIASES


class CodexAdapter(HarnessAdapter):
    name = "codex"

    def __init__(self, binary: str = "codex"):
        self.binary = binary

    def capabilities(self) -> Capabilities:
        # Subagents come from TOML agent definitions; MCP works as client and server.
        return Capabilities(json_events=True, schema_output=True, native_resume=True,
                            subagents=True, mcp=True)

    def build_cmd(self, prompt, *, model=None, effort=None, resume_session_id=None,
                  schema_path=None, system_prompt_path=None, allowed_tools=None):
        # Codex has no per-tool allow-list; sandbox mode defines capabilities.
        if resume_session_id and schema_path:
            # Fail closed rather than silently dropping a constraint. The caller
            # chooses resume with later validation or a fresh constrained session.
            raise ValueError("codex: schema and resume are incompatible; choose one")
        cmd = [self.binary, "exec"]
        if resume_session_id:
            cmd += ["resume", resume_session_id]  # Subcommand, not a --resume flag.
            # ``exec resume`` requires sandbox mode as a config override.
            sandbox = ["-c", "sandbox_mode=workspace-write"]
        else:
            sandbox = ["--sandbox", "workspace-write"]
        # Autonomous headless execution with no approval prompts.
        cmd += ["--json", "-c", "approval_policy=never", *sandbox]
        # External data homes may not be Git repositories.
        cmd += ["--skip-git-repo-check"]
        # Omit incompatible Claude-family names to use the account default.
        if model and not _claude_family(model):
            cmd += ["--model", model]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]  # Config override, not --effort.
        if system_prompt_path:
            cmd += ["-c", f"model_instructions_file={system_prompt_path}"]
        if schema_path:
            schema = Path(schema_path).read_text("utf-8").strip()
            if not schema:
                raise ValueError(f"empty schema {schema_path}; refusing an unconstrained call")
            cmd += ["--output-schema", schema_path]  # Codex expects a schema path.
        cmd.append(prompt)
        return cmd

    def parse_output(self, stdout, exit_code) -> RunResult:
        """Parse a JSONL stream rather than Claude's single JSON object.

        Join text blocks from agent messages, map ``thread_id`` to ``session_id``,
        and retain usage in ``raw``. Invalid lines are skipped. If no valid JSON
        line exists, return an unsuccessful result with the original output.
        """
        text_parts: list[str] = []
        usage = err = session_id = None
        parsed_any = False
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(ev, dict):
                continue
            parsed_any = True
            t = ev.get("type")
            if t == "thread.started":
                session_id = ev.get("thread_id")  # Codex resume identifier.
                continue
            item = ev.get("item") if isinstance(ev.get("item"), dict) else ev
            itype = item.get("item_type") or item.get("type")
            if t == "item.completed" and itype == "agent_message":
                txt = item.get("text")
                if txt is None:
                    txt = item.get("content")
                if isinstance(txt, list):
                    txt = "".join(b.get("text", "") for b in txt if isinstance(b, dict))
                if txt:
                    text_parts.append(str(txt))
            elif t == "turn.completed":
                usage = ev.get("usage")
            elif t == "error" or ev.get("is_error"):
                err = str(ev.get("message") or ev.get("error") or "codex error")
        if not parsed_any:
            # Preserve output from a crash before JSONL format initialization.
            return RunResult(ok=False, text=stdout or "", exit_code=exit_code)
        ok = exit_code == 0 and err is None
        # Keep error text in raw so the shared classifier can see quota markers
        # without duplicating classification logic across adapters.
        return RunResult(ok=ok, text="\n".join(text_parts), exit_code=exit_code,
                         session_id=session_id, cost_usd=None,
                         raw={"usage": usage, "error": err or ""})
