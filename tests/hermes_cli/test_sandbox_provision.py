"""Flipping the sandbox on provisions the kit and the shell, and stays off when it cannot.

The provision runs through pm, so a sealed install (whose store is read-only) and a
failed download both leave ``terminal.backend`` untouched and return the reason.
"""

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from hermes_cli.config import load_config  # noqa: E402
from tools.environments import mxc_host  # noqa: E402


def _available(**overrides):
    record = {
        "platform_supported": True, "available": True, "degraded": False, "reason": None, "warnings": [],
        "tier": "base-container", "wxc_exec_path": "C:\\store\\wxc-exec.exe",
        "shell_path": "C:\\store\\busybox-sh.exe", "shell_missing": False, "os_build": "10.0.28120",
        "enabled": False, "policy": {"readwrite_paths": [], "readonly_paths": [], "network": False},
        "containers_started": 0, "probe": {"ok": True},
    }
    record.update(overrides)
    return record


@pytest.fixture
def client(_isolate_hermes_home, monkeypatch, tmp_path):
    monkeypatch.setattr(mxc_host, "status", lambda **_: _available())
    default = tmp_path / "default-workspace"
    default.mkdir()
    monkeypatch.setattr(mxc_host, "default_workspace", lambda: str(default))
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app
    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


def test_enabling_provisions_both_pins_before_flipping_the_backend(client, monkeypatch):
    from hermes_cli.web_routers import sandbox as sandbox_routes

    provisioned = []
    monkeypatch.setattr(sandbox_routes, "provision_sandbox_bins", lambda: provisioned.append(True))

    resp = client.post("/api/sandbox/policy", json={"enabled": True})
    assert resp.status_code == 200
    assert provisioned == [True]
    assert load_config()["terminal"]["backend"] == "mxc"


def test_enabling_stays_off_when_provisioning_fails(client, monkeypatch):
    from hermes_cli.web_routers import sandbox as sandbox_routes
    from pm.package import InstallError

    def fail():
        raise InstallError("mxc-kit", "download failed: connection reset")

    monkeypatch.setattr(sandbox_routes, "provision_sandbox_bins", fail)

    resp = client.post("/api/sandbox/policy", json={"enabled": True})
    assert resp.status_code == 400
    assert "download failed" in resp.json()["detail"]
    assert load_config()["terminal"].get("backend", "local") != "mxc"


def test_enabling_stays_off_on_a_sealed_install(client, monkeypatch):
    from hermes_cli.web_routers import sandbox as sandbox_routes

    monkeypatch.setattr(sandbox_routes, "provision_sandbox_bins",
                        lambda: (_ for _ in ()).throw(sandbox_routes.SealedInstall(["mxc-kit"])))

    resp = client.post("/api/sandbox/policy", json={"enabled": True})
    assert resp.status_code == 400
    assert "sealed" in resp.json()["detail"].lower()
    assert load_config()["terminal"].get("backend", "local") != "mxc"
