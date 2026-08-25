"""Tests for lazy-backend refresh venv repair (#57828 / #58004)."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import hermes_cli.main as m
import pytest






def test_capture_active_tool_dependencies_uses_tools_status_probes(monkeypatch):
    from hermes_cli import tools_config

    monkeypatch.setattr(
        tools_config,
        "_module_installed",
        lambda module: module in {"langfuse", "ddgs"},
    )

    assert m._capture_active_tool_dependencies() == ["ddgs", "langfuse"]


def test_restore_active_tool_dependencies_uses_static_allowlist(monkeypatch):
    calls = []
    monkeypatch.setattr(
        m,
        "_run_install_with_heartbeat",
        lambda cmd, *, env=None: calls.append((cmd, env)),
    )

    env = {"VIRTUAL_ENV": "/tmp/venv"}
    m._restore_active_tool_dependencies(
        ["langfuse", "not-allowlisted"],
        ["uv", "pip"],
        env=env,
    )

    assert calls == [(["uv", "pip", "install", "langfuse", "--quiet"], env)]


def test_cmd_update_captures_and_propagates_pre_rebuild_snapshot(
    tmp_path, monkeypatch
):
    """The updater must carry pre-rebuild state into its repair refresh."""
    from hermes_cli import update_cmd

    (tmp_path / ".git").mkdir()
    snapshot = ["platform.telegram"]
    tool_snapshot = ["langfuse"]
    refresh_calls = []
    restore_calls = []

    class RestoreReached(Exception):
        pass

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
        if "rev-list" in cmd:
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_sync(extras=None, *, explicit=False):
        refresh_calls.append((sorted(extras or []), explicit))

    def fake_restore(dependencies, prefix, *, env=None):
        restore_calls.append((dependencies, prefix, env))
        raise RestoreReached

    monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(m, "_capture_active_lazy_features", lambda: snapshot.copy())
    monkeypatch.setattr(
        m, "_capture_active_tool_dependencies", lambda: tool_snapshot.copy()
    )
    monkeypatch.setattr(m, "_is_windows", lambda: False)
    monkeypatch.setattr(m, "_run_pre_update_backup", lambda args: None)
    monkeypatch.setattr(m, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(m, "_resume_windows_gateways_after_update", lambda state: None)
    monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *args: None)
    monkeypatch.setattr(m, "_get_origin_url", lambda *args: "https://github.com/NousResearch/hermes-agent.git")
    monkeypatch.setattr(m, "_resolve_update_branch", lambda args: "main")
    monkeypatch.setattr(m, "_stash_local_changes_if_needed", lambda *args: None)
    monkeypatch.setattr(update_cmd, "_invalidate_update_cache", lambda: None)
    monkeypatch.setattr(
        update_cmd, "_venv_core_imports_healthy", lambda: (False, "broken")
    )
    monkeypatch.setattr(update_cmd, "_write_update_incomplete_marker", lambda: None)
    monkeypatch.setattr(
        m, "_install_python_dependencies_with_optional_fallback", lambda *a, **k: None
    )
    monkeypatch.setattr(m, "_restore_active_tool_dependencies", fake_restore)
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    import pm
    from pm.packages import uv_env as _uv_env

    def fake_uv(**kw):
        env = _uv_env()
        if kw.get("venv"):
            env["VIRTUAL_ENV"] = str(kw["venv"])
        return "uv", env

    monkeypatch.setattr(pm, "uv", fake_uv)
    monkeypatch.setattr(pm, "sync_venv", fake_sync)

    args = SimpleNamespace(
        yes=True,
        force=False,
        force_venv=False,
        no_backup=True,
        backup=False,
        branch=None,
    )
    with pytest.raises(RestoreReached):
        m._cmd_update_impl(args, gateway_mode=False)

    # The repair phase is one explicit pm sync carrying the pre-rebuild
    # extras snapshot; tool-dep restore still runs against the managed env
    # (#83914: UV vars stripped, VIRTUAL_ENV pointed at the install's venv).
    from pm.packages import uv_env as managed_python_env

    expected_env = managed_python_env()
    expected_env["VIRTUAL_ENV"] = str(tmp_path / "venv")
    assert refresh_calls == [(sorted(["all", *snapshot]), True)]
    assert restore_calls == [
        (
            tool_snapshot,
            ["uv", "pip"],
            expected_env,
        )
    ]










