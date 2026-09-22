"""The file-tools env creation path applies the container cwd guard through the same predicate the
terminal tool uses, so a plugin backend declaring ``is_container`` gets a host-path override
sanitized exactly like docker does (#101013)."""

import tools.file_tools as ft
import tools.terminal_tool as terminal_tool
import tools.terminal_tool_config as ttc


def _capture_create(monkeypatch, backend):
    import json
    from types import SimpleNamespace
    from hermes_cli.config import get_config_path
    get_config_path().write_text(json.dumps({"terminal": {"backend": backend}}), encoding="utf-8")
    seen = {}

    def _fake_create(config, env_type, **kwargs):
        seen["cwd"] = kwargs["cwd"]
        return SimpleNamespace(cwd=kwargs["cwd"], env_type=env_type)

    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(ft, "_file_ops_cache", {})
    monkeypatch.setattr(terminal_tool, "_create_configured_env", _fake_create)
    monkeypatch.setattr(terminal_tool, "_select_image", lambda *a, **k: None)
    monkeypatch.setattr(terminal_tool, "_resolve_task_host_cwd", lambda *a, **k: None)
    monkeypatch.setattr(terminal_tool, "get_session_cwd", lambda _tid: None)
    return seen


def test_plugin_container_backend_gets_the_same_host_cwd_guard_as_docker(monkeypatch):
    host_cwd = "/Users/me/workspace"  # a host-shaped path (_HOST_CWD_PREFIXES), never valid in-sandbox
    monkeypatch.setattr(terminal_tool, "_get_env_config",
                        lambda: {"env_type": "mycloud", "cwd": "/workspace", "timeout": 60})
    monkeypatch.setattr(terminal_tool, "resolve_task_overrides", lambda _tid: {"cwd": host_cwd})
    monkeypatch.setattr(ttc, "_plugin_env_flag", lambda env_type, attr, default=False: attr == "is_container")
    seen = _capture_create(monkeypatch, "mycloud")

    ops = ft._get_file_ops("sess")

    assert ops.env.env_type == "mycloud"
    assert seen["cwd"] == "/workspace"  # host override dropped, not fed to the sandbox


def test_plugin_non_container_backend_keeps_the_host_cwd(monkeypatch, tmp_path):
    host_cwd = str(tmp_path / "proj")
    monkeypatch.setattr(terminal_tool, "_get_env_config",
                        lambda: {"env_type": "myremote", "cwd": "/workspace", "timeout": 60})
    monkeypatch.setattr(terminal_tool, "resolve_task_overrides", lambda _tid: {"cwd": host_cwd})
    monkeypatch.setattr(ttc, "_plugin_env_flag", lambda env_type, attr, default=False: False)
    seen = _capture_create(monkeypatch, "myremote")

    ft._get_file_ops("sess")

    assert seen["cwd"] == host_cwd
