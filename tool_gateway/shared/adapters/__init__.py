"""Agent CLI adapters and their name-based factory."""
from .base import Capabilities, HarnessAdapter, RunResult, STOP_TO_EXIT
from .claude import ClaudeAdapter
from .codex import CodexAdapter

ADAPTERS = {"claude": ClaudeAdapter, "codex": CodexAdapter}


def make_adapter(name: str) -> HarnessAdapter:
    """Build a named adapter and list valid names when the name is unknown."""
    try:
        return ADAPTERS[name]()
    except KeyError:
        raise ValueError(f"unknown harness {name!r} (available: {sorted(ADAPTERS)})") from None


__all__ = ["ADAPTERS", "Capabilities", "ClaudeAdapter", "CodexAdapter",
           "HarnessAdapter", "RunResult", "STOP_TO_EXIT", "make_adapter"]
