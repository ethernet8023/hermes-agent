"""MXC admits reviewed execution paths, not a denylist of host toolsets."""
import json

import pytest


def _policy(backend="mxc", network=False):
    from hermes_cli.config import get_config_path
    get_config_path().write_text(json.dumps({"terminal": {
        "backend": backend, "mxc_network": network,
        "mxc_readwrite_paths": [], "mxc_readonly_paths": [],
    }}), encoding="utf-8")


@pytest.mark.parametrize("network", [False, True])
def test_registry_refuses_uncontained_and_unknown_tools_before_dispatch(network):
    import model_tools

    _policy(network=network)
    for name in ("browser_exec", "browser_cdp", "browser_dialog", "computer_use",
                 "execute_code", "text_to_speech", "connectors__fixture__read",
                 "mcp_fixture_read", "unreviewed_fixture_tool"):
        _, blocked = model_tools._pre_dispatch_guards(name, {}, True, model_tools._CallIds(), [])
        assert blocked is not None, f"{name} retained host authority with network={network}"
        assert "sandbox" in json.loads(blocked[0])["error"].lower()


@pytest.mark.parametrize("entrypoint", ["inline", "managed"])
def test_inline_and_managed_dispatch_use_the_same_admission(entrypoint):
    from types import SimpleNamespace
    from agent.agent_runtime_helpers import invoke_tool
    from agent.tool_executor import _dispatch_authorized_once, _ManagedToolResult, _ToolCallRef

    _policy(network=True)
    called = []

    def host_action(args):
        called.append(args)
        return json.dumps({"ok": True})

    agent = SimpleNamespace(
        session_id="sandbox-a", _memory_manager=None, drive_preview_callback=host_action,
        _tool_guardrails=SimpleNamespace(before_call=lambda *_: SimpleNamespace(allows_execution=True)),
        _touch_activity=lambda *_: None,
    )
    args = {"action": "reload"}
    if entrypoint == "inline":
        result = invoke_tool(
            agent, "drive_preview", args, "sandbox-a", pre_tool_block_checked=True,
            skip_tool_request_middleware=True, skip_tool_execution_middleware=True)
    else:
        state = _ManagedToolResult(None, args, [], False, False)
        result = _dispatch_authorized_once(
            agent, state, _ToolCallRef("drive_preview", args, "sandbox-a", "call-1", []),
            execute=host_action, scope_block=None, display_index=None,
            begin_execution=lambda callback=None: None, authorization_gate=None)
    assert called == [], "inline dispatch must not retain host authority"
    assert "sandbox" in json.loads(result)["error"].lower()


@pytest.mark.parametrize("network", [False, True])
def test_supported_tools_remain_available_and_network_permission_is_narrow(network):
    from tools.environments.mxc_policy import tool_refusal

    _policy(network=network)
    for name in ("terminal", "read_file", "write_file", "patch", "search_files", "clarify", "todo_list"):
        assert tool_refusal(name, {}) is None, name
    assert (tool_refusal("web_search", {}) is None) is network
    assert tool_refusal("browser_exec", {}) is not None
    _policy(backend="local", network=network)
    assert tool_refusal("unreviewed_fixture_tool", {}) is None


def test_catalog_does_not_offer_uncontained_connector_or_browser_brokers(monkeypatch):
    import model_tools
    from tools import tool_search

    _policy(network=True)
    names = ("process_manage", "manage_connections", "browser_cdp")
    definitions = [{"type": "function", "function": {"name": name}} for name in names]
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **_: definitions)
    seen = []

    def catalog(args, current_tool_defs):
        seen.extend(td["function"]["name"] for td in current_tool_defs)
        return json.dumps({"tools": seen})

    monkeypatch.setattr(tool_search, "dispatch_tool_search", catalog)
    model_tools.handle_function_call("tool_search", {"queries": ["browser"]})
    assert seen == ["process_manage"]


def test_nested_host_call_is_refused_before_catalog_or_connector_discovery(monkeypatch):
    import model_tools

    _policy()
    catalog_reads = []
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **_: catalog_reads.append(True) or [])
    result = json.loads(model_tools.handle_function_call("tool_call", {"calls": [{
        "name": "browser_cdp", "arguments": {"method": "Page.navigate", "params": {"url": "https://example.com"}},
    }]}))
    assert catalog_reads == []
    assert "outside the sandbox" in result["error"]


def test_refused_action_does_not_enter_relay_execution(monkeypatch):
    from types import SimpleNamespace
    from agent import relay_tools
    from agent.tool_executor import _run_agent_tool_execution_middleware

    _policy(network=True)
    calls = []

    def relay(name, args, dispatch, **kwargs):
        calls.append(name)
        return json.dumps({"host_handler_reached": True}), args

    monkeypatch.setattr(relay_tools, "execute", relay)
    result = _run_agent_tool_execution_middleware(
        SimpleNamespace(session_id="sandbox-relay"), function_name="browser_exec",
        function_args={"code": "print('probe')"}, effective_task_id="sandbox-relay",
        tool_call_id="call-relay", execute=lambda _: None)
    assert calls == []
    assert result.blocked
    assert "sandbox" in json.loads(result.result)["error"].lower()


def test_inactive_sandbox_grants_do_not_disable_local_tools(tmp_path):
    from hermes_cli.config import get_config_path
    from tools.environments.mxc_policy import tool_refusal

    get_config_path().write_text(json.dumps({"terminal": {
        "backend": "local", "mxc_readwrite_paths": [str(tmp_path / "removed-folder")],
    }}), encoding="utf-8")
    assert tool_refusal("browser_exec", {}) is None


def test_url_gate_fails_closed_without_exposing_invalid_policy_contents():
    from hermes_cli.config import get_config_path
    from tools.website_policy import check_website_access

    get_config_path().write_text("terminal: [\napi_key: DO_NOT_EXPOSE_FIXTURE\n", encoding="utf-8")
    result = check_website_access("https://example.com")
    assert result is not None and result["source"] == "sandbox"
    assert "DO_NOT_EXPOSE_FIXTURE" not in json.dumps(result)


def test_explicit_website_blocklist_does_not_bypass_active_sandbox_policy(tmp_path):
    from tools.website_policy import check_website_access

    _policy(network=False)
    website_config = tmp_path / "website-only.yaml"
    website_config.write_text("website_blocklist:\n  enabled: false\n", encoding="utf-8")
    result = check_website_access("https://example.com", config_path=website_config)
    assert result is not None and result["source"] == "sandbox"


def test_retirement_errors_are_sanitized_and_admission_retries(monkeypatch):
    from unittest.mock import Mock
    import model_tools
    from hermes_constants import hermes_home_key
    from tools import terminal_policy_lifecycle as lifecycle
    from tools.environments.mxc_policy import tool_refusal

    _policy(network=True)
    owner = hermes_home_key()
    retire = Mock(side_effect=RuntimeError("DO_NOT_EXPOSE_RETIREMENT_FIXTURE"))
    monkeypatch.setattr(lifecycle, "_retire_workers", retire)
    for _ in range(2):
        result = json.loads(model_tools.handle_function_call("browser_exec", {}))
        assert "policy is unavailable" in result["error"]
        assert "DO_NOT_EXPOSE" not in json.dumps(result)
        assert owner in lifecycle._failed
        assert owner not in lifecycle._applied
    assert retire.call_count == 2

    retire.side_effect = None
    assert tool_refusal("web_search", {}) is None
    assert retire.call_count == 3
    assert owner not in lifecycle._failed
    assert owner in lifecycle._applied


def test_catalog_snapshot_retains_project_discovery_but_not_mutation(monkeypatch):
    from unittest.mock import Mock
    import model_tools
    from tools import project_tools  # register the actual project schema
    from tools.environments import mxc_policy

    _policy(network=True)
    definitions = [{"type": "function", "function": model_tools.registry.get_schema("desktop_project")},
                   {"type": "function", "function": {"name": "process_manage"}},
                   {"type": "function", "function": {"name": "browser_cdp"}}]
    original = json.dumps(definitions, sort_keys=True)
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **_: definitions)
    snapshot = Mock(wraps=mxc_policy._active_settings)
    monkeypatch.setattr(mxc_policy, "_active_settings", snapshot)
    described, underlying = model_tools._dispatch_bridge_tool(
        "tool_describe", {"names": ["desktop_project", "browser_cdp"]}, None, None)
    result = json.loads(described)
    snapshot.assert_called_once()
    assert "desktop_project" in result["tools"]
    assert "browser_cdp" not in result["tools"]
    assert underlying is None
    assert json.dumps(definitions, sort_keys=True) == original

    listed = json.loads(model_tools.handle_function_call("tool_call", {"calls": [{
        "name": "desktop_project", "arguments": {"action": "list"},
    }]}))
    assert listed["projects"] == []
    for action in ("create", "switch"):
        refused = json.loads(model_tools.handle_function_call("tool_call", {"calls": [{
            "name": "desktop_project", "arguments": {"action": action, "name": "Fixture"},
        }]}))
        assert "outside the sandbox" in refused["error"]
    assert json.loads(project_tools.project_list())["projects"] == []
