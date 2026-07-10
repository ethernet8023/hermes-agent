"""Shared helpers for classifying tool result payloads."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any


FILE_MUTATING_TOOL_NAMES = frozenset({"write_file", "patch"})


class ToolEffectDisposition(str, Enum):
    """What Hermes knows about a tool call's externally visible effects."""

    COMMITTED = "committed"
    NONE = "none"
    UNKNOWN = "unknown"
    PARTIAL = "partial"
    ROLLED_BACK = "rolled_back"


# Known observational tools. Unknown/plugin/MCP tools are conservatively treated
# as effect-capable so an orphan is not silently presented as never having run.
NO_EFFECT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "session_search", "skill_view", "skills_list",
    "web_extract", "web_search", "vision_analyze", "browser_snapshot",
    "browser_get_images", "browser_console", "todo", "read_terminal",
})


def tool_may_have_side_effect(tool_name: str) -> bool:
    return tool_name not in NO_EFFECT_TOOL_NAMES


def classify_tool_effect(
    tool_name: str,
    result: Any,
    *,
    status: str = "completed",
) -> ToolEffectDisposition:
    """Classify effects independently from a tool's success/failure status."""
    normalized = (status or "completed").strip().lower()
    if normalized in {"blocked", "skipped", "not_started"}:
        return ToolEffectDisposition.NONE
    if not tool_may_have_side_effect(tool_name):
        return ToolEffectDisposition.NONE
    if normalized in {
        "timeout", "timed_out", "detached", "cancelled", "interrupted",
        "error", "failed",
    }:
        return ToolEffectDisposition.UNKNOWN
    if normalized == "partial":
        return ToolEffectDisposition.PARTIAL
    if normalized in {"rolled_back", "rollback"}:
        return ToolEffectDisposition.ROLLED_BACK
    return ToolEffectDisposition.COMMITTED


def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """Return True when a file mutation result proves the write landed."""
    if tool_name not in FILE_MUTATING_TOOL_NAMES or not isinstance(result, str):
        return False
    try:
        data = json.loads(result.strip())
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    if tool_name == "write_file":
        return "bytes_written" in data
    if tool_name == "patch":
        return data.get("success") is True
    return False
