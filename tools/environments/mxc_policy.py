"""Admission for agent actions under MXC's strict execution policy.

Tool definitions stay stable. Only reviewed execution paths are admitted while
containment is enabled; an unfamiliar plugin/tool is not implicitly trusted host code.
Configuration authority remains in terminal_scope through mxc_host, not in this table.
"""
from __future__ import annotations

import logging

from tools.environments import mxc_host

logger = logging.getLogger(__name__)

# These operations execute through the selected environment. Process management
# additionally checks the recorded process owner/backend at its handler.
_ENVIRONMENT_TOOLS = frozenset({
    "terminal", "read_file", "write_file", "patch", "search_files", "process_manage",
})
# Fixed-purpose Hermes services remain on the host. They do not expose an arbitrary
# process or filesystem API; inline skill commands have a separate execution guard.
_HOST_SERVICES = frozenset({
    "clarify", "todo_list", "memory", "session_search", "skills_list", "skill_view",
    "skill_manage", "delegate_task", "vision_analyze",
})
_CATALOG_TOOLS = frozenset({"tool_search", "tool_describe", "tool_call"})
_WEB_SERVICES = frozenset({"web_search", "web_extract"})
_POLICY_UNAVAILABLE = (
    "Sandbox policy is unavailable for this profile. No agent action was run. "
    "Check the profile's terminal settings before retrying."
)


def _active_settings():
    """One policy snapshot for the decision; invalid authority is a refusal."""
    try:
        from tools.terminal_policy_lifecycle import reconcile_terminal_policy
        from tools.terminal_scope import get_live_terminal_config
        # Even a refused action must retire work admitted under an older policy.
        reconcile_terminal_policy()
        config = get_live_terminal_config()
        if not mxc_host.backend_enabled(config):
            return None, None
        return mxc_host.resolve_settings(config), None
    except Exception:
        # Retirement can raise arbitrary backend errors. Keep details in the log,
        # never in the refusal (parser excerpts may contain profile secrets).
        logger.debug("Sandbox policy admission failed", exc_info=True)
        return None, _POLICY_UNAVAILABLE


def _host_refusal(action: str) -> str:
    return (f"{action} is unavailable while the MXC sandbox is on because it can act "
            "on the host outside the sandbox. Use the sandboxed terminal or file tools "
            "for filesystem work.")


def uncontained_action_refusal(action: str) -> str | None:
    """Guard non-tool host execution/export paths, including scheduled scripts."""
    settings, error = _active_settings()
    return error or (_host_refusal(action) if settings is not None else None)


def network_refusal() -> str | None:
    """Network admission for reviewed host fetchers and context expansion."""
    settings, error = _active_settings()
    if error:
        return error
    return mxc_host.OFFLINE_REASON if settings is not None and not settings.policy.network else None


def tool_refusal(tool_name: str, args: dict) -> str | None:
    """The same live admission decision for inline, registry and nested tool calls."""
    settings, error = _active_settings()
    if error or settings is None:
        return error
    return _tool_refusal(settings, tool_name, args)


def filter_catalog(definitions: list[dict]) -> list[dict]:
    """Filter one discovery result against one snapshot, without changing schemas.

    Action-scoped tools stay discoverable when any reviewed action is admitted;
    invocation still checks the actual arguments against live policy.
    """
    settings, error = _active_settings()
    if error:
        return []
    if settings is None:
        return definitions
    allowed = []
    for definition in definitions:
        name = (definition.get("function") or definition).get("name", "")
        args = {"action": "list"} if name == "desktop_project" else {}
        if _tool_refusal(settings, name, args) is None:
            allowed.append(definition)
    return allowed


def _tool_refusal(settings: mxc_host.MxcSettings, tool_name: str, args: dict) -> str | None:
    """Classify arguments against the caller's already-resolved MXC snapshot."""
    if tool_name == "tool_call":
        from tools.tool_search import normalize_tool_call_entries
        calls, validation_error = normalize_tool_call_entries(args)
        if not validation_error:
            for call in calls:
                if call["name"] in _CATALOG_TOOLS:
                    return _host_refusal("Nested tool catalog calls")
                refusal = _tool_refusal(settings, call["name"], call["arguments"])
                if refusal is not None:
                    return refusal
        # The bridge owns malformed-call diagnostics and will not dispatch them.
        return None
    if tool_name in _ENVIRONMENT_TOOLS or tool_name in _HOST_SERVICES or tool_name in _CATALOG_TOOLS:
        return None
    if tool_name == "desktop_project" and args.get("action") == "list":
        return None
    if tool_name in _WEB_SERVICES:
        return None if settings.policy.network else mxc_host.OFFLINE_REASON
    return _host_refusal(tool_name)
