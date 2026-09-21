"""Sandbox routes: the desktop's Safety > Sandbox panel reads one status record and writes policy
through config.yaml's ``terminal`` section. These tests stub the host probe so they run on every
platform; the live container contract is covered by the ``windows_only`` environment tests.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from hermes_cli.config import load_config  # noqa: E402
from tools.environments import mxc_host  # noqa: E402


def _available(**overrides):
    record = {
        "platform_supported": True, "available": True, "degraded": False, "reason": None, "warnings": [],
        "tier": "base-container", "wxc_exec_path": "C:\\mxc-kit\\bin\\wxc-exec.exe",
        "shell_path": "C:\\hermes\\bin\\busybox-sh.exe", "shell_missing": False, "os_build": "10.0.28120",
        "enabled": False, "policy": {"readwrite_paths": [], "readonly_paths": [], "network": False},
        "containers_started": 0, "probe": {"ok": True},
    }
    record.update(overrides)
    return record


@pytest.fixture
def client(_isolate_hermes_home, monkeypatch, tmp_path):
    monkeypatch.setattr(mxc_host, "status", lambda **_: _available())
    # Real ancestor_readiness over a stubbed icacls, so the route returns the genuine record shape
    # the desktop dereferences (a hand-shaped stub here once hid a missing field).
    listing = "X APPLICATION PACKAGE AUTHORITY\\ALL APPLICATION PACKAGES:(R)\n"
    monkeypatch.setattr(mxc_host, "_icacls",
                        lambda directory, *args, **kw: __import__("subprocess").CompletedProcess([], 0, listing, ""))
    # The default workspace lives in the REAL user profile; tests must never create it there.
    default = tmp_path / "default-workspace"
    default.mkdir()
    monkeypatch.setattr(mxc_host, "default_workspace", lambda: str(default))
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


def test_status_reports_availability_policy_and_the_requested_workspace(client, tmp_path):
    # A sibling of the isolated HERMES_HOME: tmp_path itself contains it and would be refused.
    project = tmp_path / "proj"
    project.mkdir()
    resp = client.get("/api/sandbox/status", params={"workspace": str(project)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True and body["enabled"] is False
    assert body["workspace"] == os.path.normpath(str(project))
    assert set(body["workspace_ancestors"]) == {"ready", "missing", "needs_admin", "admin_command"}
    assert body["workspace_ancestors"]["ready"] is True


def test_status_shows_the_default_workspace_for_a_session_anchored_at_home(client, tmp_path):
    resp = client.get("/api/sandbox/status", params={"workspace": os.path.expanduser("~")})
    assert resp.status_code == 200
    assert resp.json()["workspace"] == str(tmp_path / "default-workspace")


def test_fs_default_cwd_lands_a_fresh_draft_in_the_default_workspace_under_mxc(client, tmp_path, monkeypatch):
    from hermes_cli.web_routers import files
    default = str(tmp_path / "default-workspace")
    monkeypatch.setattr(files, "load_config", lambda: {"terminal": {"backend": "mxc", "cwd": os.path.expanduser("~")}})
    assert files._fs_default_cwd() == default
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(files, "load_config", lambda: {"terminal": {"backend": "mxc", "cwd": str(project)}})
    assert os.path.normcase(files._fs_default_cwd()) == os.path.normcase(str(project.resolve()))
    monkeypatch.setattr(files, "load_config", lambda: {"terminal": {"backend": "local", "cwd": os.path.expanduser("~")}})
    assert os.path.normcase(files._fs_default_cwd()) == os.path.normcase(os.path.realpath(os.path.expanduser("~")))


def test_enabling_switches_the_terminal_backend_and_disabling_restores_local(client, monkeypatch):
    import tools.terminal_tool as terminal_tool
    from hermes_cli.web_routers import sandbox as sandbox_routes

    class _Env:
        cleaned = 0

        def cleanup(self):
            _Env.cleaned += 1

    bridged = []
    monkeypatch.setattr(sandbox_routes, "_apply_backend_switch_to_this_process",
                        lambda: bridged.append(True) or terminal_tool._active_environments.clear())
    terminal_tool._active_environments["task-a"] = _Env()

    resp = client.post("/api/sandbox/policy", json={"enabled": True})
    assert resp.status_code == 200
    assert load_config()["terminal"]["backend"] == "mxc"
    assert bridged == [True], "a backend change must be applied to the running process"
    assert "task-a" not in terminal_tool._active_environments

    resp = client.post("/api/sandbox/policy", json={"network": True})
    assert resp.status_code == 200
    assert bridged == [True], "a policy-only edit is live already and must not evict environments"

    resp = client.post("/api/sandbox/policy", json={"enabled": False})
    assert resp.status_code == 200
    assert load_config()["terminal"]["backend"] == "local"
    assert bridged == [True, True]


def test_live_backend_switch_rebridges_env_and_evicts_cached_environments(monkeypatch, _isolate_hermes_home):
    import tools.terminal_tool as terminal_tool
    from hermes_cli.web_routers import sandbox as sandbox_routes

    class _Env:
        cleaned = 0

        def cleanup(self):
            _Env.cleaned += 1

    calls = []
    monkeypatch.setattr("hermes_cli.config.apply_terminal_config_to_env", lambda env=None: calls.append(env))
    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: False)
    terminal_tool._active_environments["task-b"] = _Env()
    try:
        sandbox_routes._apply_backend_switch_to_this_process()
    finally:
        terminal_tool._active_environments.pop("task-b", None)
    assert calls == [None] and _Env.cleaned == 1


def test_enabling_is_refused_with_the_host_reason_when_mxc_cannot_run(client, monkeypatch):
    monkeypatch.setattr(mxc_host, "status",
                        lambda **_: _available(available=False, reason="This Windows build does not support MXC."))
    resp = client.post("/api/sandbox/policy", json={"enabled": True})
    assert resp.status_code == 400
    assert "does not support MXC" in resp.json()["detail"]
    assert load_config()["terminal"].get("backend", "local") != "mxc"


def test_policy_edits_persist_normalized_paths_and_network(client, tmp_path):
    ro = tmp_path / "Docs"
    rw = tmp_path / "Proj"
    ro.mkdir()
    rw.mkdir()
    resp = client.post("/api/sandbox/policy", json={
        "readwrite_paths": [str(rw), str(rw).upper(), ""],
        "readonly_paths": [str(ro) + os.sep],
        "network": True,
    })
    assert resp.status_code == 200
    terminal = load_config()["terminal"]
    assert [p.lower() for p in terminal["mxc_readwrite_paths"]] == [os.path.normpath(str(rw)).lower()]
    assert terminal["mxc_readonly_paths"] == [os.path.normpath(str(ro))]
    assert terminal["mxc_network"] is True


def test_grant_adds_the_folder_of_a_file_and_readwrite_supersedes_readonly(client, tmp_path):
    folder = tmp_path / "Documents"
    folder.mkdir()
    target = folder / "taxes.pdf"
    target.write_text("x", encoding="utf-8")

    resp = client.post("/api/sandbox/grant", json={"path": str(target), "mode": "read"})
    assert resp.status_code == 200
    assert resp.json()["granted"] == os.path.normpath(str(folder))
    assert load_config()["terminal"]["mxc_readonly_paths"] == [os.path.normpath(str(folder))]

    resp = client.post("/api/sandbox/grant", json={"path": str(folder), "mode": "readwrite"})
    assert resp.status_code == 200
    terminal = load_config()["terminal"]
    assert terminal["mxc_readwrite_paths"] == [os.path.normpath(str(folder))]
    assert terminal["mxc_readonly_paths"] == []


def test_grant_rejects_relative_and_missing_paths(client, tmp_path):
    assert client.post("/api/sandbox/grant", json={"path": "relative/dir", "mode": "read"}).status_code == 400
    assert client.post("/api/sandbox/grant", json={"path": str(tmp_path / "nope"), "mode": "read"}).status_code == 400
    assert client.post("/api/sandbox/grant", json={"path": str(tmp_path), "mode": "sideways"}).status_code == 400


def test_prepare_reports_what_it_did_and_what_needs_an_administrator(client, tmp_path, monkeypatch):
    monkeypatch.setattr(mxc_host, "prepare_ancestors", lambda path: {
        "prepared": [str(tmp_path)], "needs_admin": ["C:\\"], "admin_command": 'icacls "C:\\" ...', "errors": []})
    resp = client.post("/api/sandbox/prepare", json={"path": str(tmp_path / "proj")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["prepared"] == [str(tmp_path)] and body["needs_admin"] == ["C:\\"]
    assert body["workspace"] == os.path.normpath(str(tmp_path / "proj"))
