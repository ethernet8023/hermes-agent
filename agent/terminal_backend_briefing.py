"""Re-brief a live conversation when its terminal backend changes.

The system prompt describes the terminal backend in force when the conversation started and is
byte-stable from then on (prompt caching). A sandbox toggled on or off mid-conversation is
enforced by the next command regardless, so without this the agent keeps working from a stale
description: MSYS paths against a POSIX-on-Windows shell, or permission requests for a policy
that no longer exists. The tracker compares the backend each tool call actually ran under with
the one the agent was last briefed on and, on a change, hands back a one-time note for the
caller to append to that tool result, the same seam subdirectory AGENTS.md hints ride.
"""
from __future__ import annotations

from typing import Optional


class TerminalBackendBriefing:
    def __init__(self, briefed: str = "") -> None:
        # The backend the current system prompt describes; ``""`` until the prompt is built.
        self.briefed = briefed

    def record_prompt_backend(self, backend: str) -> None:
        """Called when the system prompt is (re)built; the prompt's backend needs no note."""
        self.briefed = backend

    def check_tool_call(self, task_id: Optional[str]) -> Optional[str]:
        """Note to append to the tool result that just ran under *task_id*'s environment, or None."""
        if not self.briefed:
            return None
        try:
            from tools.terminal_tool_lifecycle import get_active_env
            env = get_active_env(task_id or "default")
        except Exception:  # noqa: BLE001 — a briefing must never break a tool result
            return None
        current = getattr(env, "env_type", "") if env is not None else ""
        if not current or current == self.briefed:
            return None
        from agent.prompt_builder import terminal_backend_switch_note
        note = terminal_backend_switch_note(self.briefed, current)
        self.briefed = current
        return note
