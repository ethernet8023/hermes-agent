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

_REAL_STATUS = mxc_host.status


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
    assert "workspace_ancestors" not in body


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


def test_every_policy_write_reconciles_including_network_changes(client, monkeypatch):
    from hermes_cli.web_routers import sandbox
    reconciled = []
    monkeypatch.setattr(sandbox, "reconcile_terminal_policy", lambda: reconciled.append(load_config()["terminal"]["backend"]))
    for update, expected in (({"enabled": True}, "mxc"), ({"network": True}, "mxc"), ({"enabled": False}, "local")):
        response = client.post("/api/sandbox/policy", json=update)
        assert response.status_code == 200, response.text
        assert load_config()["terminal"]["backend"] == expected
    assert reconciled == ["mxc", "mxc", "local"]


def test_real_status_and_live_config_follow_profile_writes_a_b_a(client, monkeypatch, tmp_path):
    from hermes_cli import web_server_profiles
    monkeypatch.setattr(mxc_host, "status", _REAL_STATUS)
    monkeypatch.setattr(mxc_host, "_IS_WINDOWS", False)
    homes = {name: tmp_path / name for name in ("alpha", "beta")}
    for name, home in homes.items():
        home.mkdir()
        backend = "mxc" if name == "alpha" else "local"
        (home / "config.yaml").write_text(f"terminal:\n  backend: {backend}\n  mxc_network: false\n", encoding="utf-8")
    monkeypatch.setattr(web_server_profiles, "_resolve_profile_dir", lambda name: homes[name])
    for name, network in (("alpha", True), ("beta", False), ("alpha", False)):
        response = client.post("/api/sandbox/policy", params={"profile": name}, json={"network": network})
        assert response.status_code == 200, response.text
        assert response.json()["enabled"] == (name == "alpha")
        assert response.json()["policy"]["network"] == network
        read = client.get("/api/sandbox/status", params={"profile": name})
        assert read.status_code == 200, read.text
        assert read.json()["policy"] == response.json()["policy"]
    with web_server_profiles._hermes_home_scope(homes["beta"]):
        assert load_config()["terminal"]["backend"] == "local"


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
        "readwrite_paths": [str(rw), str(rw) + os.sep],
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


@pytest.mark.parametrize("endpoint,payload", [
    ("policy", {"network": True}),
    ("grant", {"mode": "read"}),
])
def test_writes_return_the_edited_profile_and_reconcile_inside_scope(client, tmp_path, monkeypatch, endpoint, payload):
    from hermes_cli.web_routers import sandbox
    from hermes_constants import get_hermes_home
    from hermes_cli import web_server_profiles

    homes = {name: tmp_path / name for name in ("alpha", "beta")}
    for home in homes.values():
        home.mkdir()
        (home / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    monkeypatch.setattr(web_server_profiles, "_resolve_profile_dir", lambda name: homes[name])
    seen = []
    monkeypatch.setattr(sandbox, "reconcile_terminal_policy", lambda: seen.append(get_hermes_home().name), raising=False)
    monkeypatch.setattr(mxc_host, "status", lambda **_: _available(owner=get_hermes_home().name))
    folder = tmp_path / "grant-folder"
    folder.mkdir()
    for name in ("alpha", "beta", "alpha"):
        body = {**payload, "profile": name}
        if endpoint == "grant":
            body["path"] = str(folder)
        response = client.post(f"/api/sandbox/{endpoint}", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["owner"] == name
    assert seen == ["alpha", "beta", "alpha"]


def test_transition_failure_is_not_reported_as_protected(client, monkeypatch):
    from hermes_cli.web_routers import sandbox
    def fail():
        raise RuntimeError("old host kernel would not stop")
    monkeypatch.setattr(sandbox, "reconcile_terminal_policy", fail, raising=False)
    response = client.post("/api/sandbox/policy", json={"enabled": True})
    assert response.status_code == 500
    assert "old host kernel would not stop" in response.json()["detail"]
    assert client.get("/api/sandbox/status").status_code == 500


def test_grant_refuses_a_target_that_changed_since_consent(client, tmp_path):
    folder = tmp_path / "scope"
    folder.mkdir()
    response = client.post("/api/sandbox/grant", json={"path": str(folder), "expected_target": str(tmp_path / "different")})
    assert response.status_code == 409
    assert load_config()["terminal"].get("mxc_readonly_paths", []) == []


def test_prepare_is_retired_without_mutating_host_acls(client, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("must not mutate ACLs")
    monkeypatch.setattr(mxc_host, "prepare_ancestors", forbidden, raising=False)
    assert client.post("/api/sandbox/prepare", json={"path": str(tmp_path)}).status_code == 410


def test_preview_and_grant_share_exact_recursive_scope(client, tmp_path, monkeypatch):
    from hermes_cli.web_routers import sandbox
    monkeypatch.setattr(sandbox, "reconcile_terminal_policy", lambda: None, raising=False)
    folder = tmp_path / "Documents"
    folder.mkdir()
    file = folder / "taxes.pdf"
    file.write_text("private", encoding="utf-8")
    preview = client.get("/api/sandbox/grant-target", params={"path": str(file)})
    assert preview.status_code == 200
    assert preview.json() == {"target": str(folder), "recursive": True}
    granted = client.post("/api/sandbox/grant", json={"path": str(file), "mode": "read"})
    assert granted.status_code == 200
    assert granted.json()["granted"] == preview.json()["target"]


@pytest.mark.parametrize("field", ["readonly_paths", "readwrite_paths"])
def test_policy_rejects_invalid_and_protected_paths_before_save(client, tmp_path, field):
    from hermes_constants import get_hermes_home
    for path in ("relative", "", str(tmp_path / "missing"), str(get_hermes_home()), str(tmp_path)):
        response = client.post("/api/sandbox/policy", json={field: [path]})
        assert response.status_code == 400, (path, response.text)
        assert load_config()["terminal"].get("mxc_" + field, []) == []


def test_preview_and_grant_reject_protected_parent(client, tmp_path):
    from hermes_constants import get_hermes_home
    protected = get_hermes_home() / "secret.txt"
    protected.write_text("fixture", encoding="utf-8")
    assert client.get("/api/sandbox/grant-target", params={"path": str(protected)}).status_code == 400
    assert client.post("/api/sandbox/grant", json={"path": str(protected)}).status_code == 400


def test_atomic_revoke_preserves_other_grants(client, tmp_path, monkeypatch):
    from hermes_cli.web_routers import sandbox
    monkeypatch.setattr(sandbox, "reconcile_terminal_policy", lambda: None, raising=False)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    for folder in (a, b):
        assert client.post("/api/sandbox/grant", json={"path": str(folder)}).status_code == 200
    response = client.request("DELETE", "/api/sandbox/grant", json={"path": str(a)})
    assert response.status_code == 200
    assert load_config()["terminal"]["mxc_readonly_paths"] == [str(b)]
