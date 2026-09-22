"""A live conversation is re-briefed on its first tool result under a changed terminal backend."""
from types import SimpleNamespace

from agent import prompt_builder
from agent.terminal_backend_briefing import TerminalBackendBriefing


def _env(env_type):
    return SimpleNamespace(env_type=env_type)


def test_no_note_until_the_prompt_backend_is_known_or_while_it_matches(monkeypatch):
    monkeypatch.setattr("tools.terminal_tool_lifecycle.get_active_env", lambda task_id: _env("mxc"))
    briefing = TerminalBackendBriefing()
    assert briefing.check_tool_call("t") is None, "no prompt built yet: nothing to compare against"
    briefing.record_prompt_backend("mxc")
    assert briefing.check_tool_call("t") is None
    monkeypatch.setattr("tools.terminal_tool_lifecycle.get_active_env", lambda task_id: None)
    assert briefing.check_tool_call("t") is None, "no environment yet (no command has run)"


def test_sandbox_turned_on_mid_conversation_is_announced_once_with_the_sandbox_notes(monkeypatch):
    monkeypatch.setattr("tools.terminal_tool_lifecycle.get_active_env", lambda task_id: _env("mxc"))
    briefing = TerminalBackendBriefing()
    briefing.record_prompt_backend("local")
    note = briefing.check_tool_call("t")
    assert note and "[Environment changed]" in note and "turned on" in note
    assert "busybox" in note and "[Sandbox]" in note, "the sandbox shell and denial notes travel with the switch"
    assert briefing.check_tool_call("t") is None, "announced once, not on every result"


def test_sandbox_turned_off_mid_conversation_is_announced_and_a_second_flip_is_announced_again(monkeypatch):
    monkeypatch.setattr("tools.terminal_tool_lifecycle.get_active_env", lambda task_id: _env("local"))
    briefing = TerminalBackendBriefing()
    briefing.record_prompt_backend("mxc")
    note = briefing.check_tool_call("t")
    assert note and "turned off" in note and "no sandbox policy" in note
    monkeypatch.setattr("tools.terminal_tool_lifecycle.get_active_env", lambda task_id: _env("mxc"))
    assert "turned on" in (briefing.check_tool_call("t") or "")


def test_switch_to_a_remote_backend_carries_that_backend_block(monkeypatch):
    """Every backend the terminal tool can build is tagged with its type at creation, so a switch to
    docker, modal, ssh or a plugin backend is announced with the same block its prompt would carry."""
    monkeypatch.setattr(prompt_builder, "_probe_remote_backend", lambda backend: "")
    note = prompt_builder.terminal_backend_switch_note("local", "docker")
    assert "changed from `local` to `docker`" in note
    assert "Terminal backend: docker" in note, "the remote-backend block the prompt would have carried"
    monkeypatch.setattr("tools.terminal_tool_lifecycle.get_active_env", lambda task_id: _env("docker"))
    briefing = TerminalBackendBriefing()
    briefing.record_prompt_backend("local")
    assert "Terminal backend: docker" in (briefing.check_tool_call("t") or "")
    monkeypatch.setattr("tools.terminal_tool_lifecycle.get_active_env", lambda task_id: _env("local"))
    back = briefing.check_tool_call("t") or ""
    assert "changed from `docker` to `local`" in back and "Host:" in back


def test_restored_prompt_rebriefs_without_changing_saved_bytes(monkeypatch):
    from agent import conversation_loop
    stored = "Stored prompt describing a previous shell"
    agent = SimpleNamespace(_terminal_backend_briefing=TerminalBackendBriefing(),
        _use_prompt_caching=False, _session_db=SimpleNamespace(get_session=lambda sid: {"system_prompt": stored}),
        session_id="restored", _cached_system_prompt=None, _bot_mode_protocol=False, platform="cli")
    monkeypatch.setattr(conversation_loop, "_stored_prompt_matches_runtime", lambda *a: True)
    monkeypatch.setattr(conversation_loop, "stage_surface_switch_note", lambda *a: False)
    conversation_loop._restore_or_build_system_prompt(agent, None, [{"role": "user", "content": "continue"}])
    monkeypatch.setattr("tools.terminal_tool_lifecycle.get_active_env", lambda task: _env("mxc"))
    note = agent._terminal_backend_briefing.check_tool_call("t")
    assert note and "[Sandbox]" in note
    assert agent._cached_system_prompt == stored
    assert agent._terminal_backend_briefing.check_tool_call("t") is None
    restored = TerminalBackendBriefing()
    restored.restore_prompt_backend(stored, [{"role": "tool", "content": "result" + note}])
    assert restored.check_tool_call("t") is None, "the durable last announcement survives another agent restore"


def test_policy_only_change_is_rebriefed_once(monkeypatch):
    from tools import terminal_scope
    monkeypatch.setattr("tools.terminal_tool_lifecycle.get_active_env", lambda task: _env("mxc"))
    policy = {"backend": "mxc", "mxc_network": True, "mxc_readwrite_paths": [], "mxc_readonly_paths": []}
    monkeypatch.setattr(terminal_scope, "get_live_terminal_config", lambda: dict(policy))
    briefing = TerminalBackendBriefing()
    briefing.record_prompt_backend("mxc")
    policy["mxc_network"] = False
    note = briefing.check_tool_call("t")
    assert note and "policy changed" in note and "network" in note.lower()
    assert briefing.check_tool_call("t") is None
