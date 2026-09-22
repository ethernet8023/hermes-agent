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
import re
import hashlib
import json


def _policy_signature() -> str:
    from tools.terminal_scope import get_live_terminal_config
    policy = get_live_terminal_config()
    values = {key: value for key, value in policy.items() if key.startswith("mxc_")}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


class TerminalBackendBriefing:
    def __init__(self, briefed: str = "") -> None:
        # The backend the current system prompt describes; ``""`` until the prompt is built.
        self.briefed = briefed
        self.policy = ""

    def record_prompt_backend(self, backend: str) -> None:
        """Called when the system prompt is (re)built; the prompt's backend needs no note."""
        self.briefed = backend
        try:
            self.policy = _policy_signature() if backend == "mxc" else ""
        except Exception:
            self.policy = ""

    def restore_prompt_backend(self, prompt: str, history=None) -> None:
        """Recover the baseline from saved bytes, never from a speculative rebuild.

        Legacy prompts without a marked runtime block get one accurate runtime
        note rather than silently assuming today's backend describes those bytes.
        """
        block = prompt.split("# Hermes runtime environment", 1)
        runtime = block[1].split("<!-- End Hermes runtime environment -->", 1)[0] if len(block) == 2 else ""
        backend = re.search(r"Terminal backend: ([\w-]+)", runtime)
        self.briefed = (backend.group(1) if backend else
                        "mxc" if "[Sandbox]" in runtime else
                        "local" if "Host:" in runtime else "unknown")
        self.policy = ""
        for message in reversed(history or []):
            if message.get("role") != "tool":
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
            marker = re.search(r"<!-- hermes-terminal-state backend=([\w-]+) policy=([a-f0-9]*) -->", str(content))
            if marker:
                self.briefed, self.policy = marker.groups()
                break

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
        if not current:
            return None
        try:
            signature = _policy_signature() if current == "mxc" else ""
        except Exception:
            return None
        if current == self.briefed and signature == self.policy:
            return None
        from agent.prompt_builder import terminal_backend_switch_note
        note = ("\n\n[Environment changed] The sandbox policy changed. Network and folder grants now follow "
                "the current policy; do not assume earlier permissions still apply."
                if current == self.briefed else terminal_backend_switch_note(self.briefed, current))
        self.briefed = current
        self.policy = signature
        return note + f"\n<!-- hermes-terminal-state backend={current} policy={signature} -->"
