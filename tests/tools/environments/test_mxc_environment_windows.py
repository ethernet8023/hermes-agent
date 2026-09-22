"""Real base-container acceptance using only disposable fixtures and an installed shell.

Never download a runtime, prepare ACLs, use fallback, or touch real profile files.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tools.environments import mxc_host
from tools.file_operations import ShellFileOperations

pytestmark = pytest.mark.platforms("windows")


def _provisioned_shell() -> str | None:
    """The sandbox shell in the pm store; tests never download it."""
    binary = mxc_host.store_binary("busybox")
    return str(binary) if binary is not None else None


@pytest.fixture
def live_env(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    from tools.environments.mxc import MxcEnvironment
    shell = _provisioned_shell()
    if shell is None:
        pytest.skip("sandbox shell not provisioned on this machine (enable the sandbox once first)")
    home = get_hermes_home()
    assert home.is_relative_to(tmp_path), "test isolation must own the active profile"
    config = home / "config.yaml"
    config.write_text(json.dumps({"terminal": {
        "backend": "mxc", "mxc_network": False, "mxc_debug": False,
        "mxc_readonly_paths": [], "mxc_readwrite_paths": [],
    }}), encoding="utf-8")
    record = mxc_host.status(provision_shell=False)
    if not record["available"]:
        pytest.skip(f"strict MXC prerequisites unavailable: {record['reason']}")
    assert record["tier"] == "base-container"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = MxcEnvironment(cwd=str(workspace), timeout=30)
    try:
        yield env
    finally:
        env.cleanup()


def test_default_read_reports_denial_for_existing_fake_credentials(live_env, tmp_path):
    from hermes_constants import get_hermes_home
    secret = get_hermes_home() / ".env"
    secret.write_text("FAKE_CREDENTIAL_FIXTURE_ONLY\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    witness = outside / "witness.txt"
    witness.write_text("OUTSIDE_FIXTURE_ONLY\n", encoding="utf-8")
    ops = ShellFileOperations(live_env)
    search = ops.search("*", path=str(outside), target="files")
    assert search.error and "refused" in search.error and "[Sandbox]" in search.error
    for target in (secret, witness):
        assert target.is_file()
        result = ops.read_file(str(target))
        assert result.error and "refused" in result.error, result.to_dict()
        assert "[Sandbox]" in result.error
        assert "File not found" not in result.error
        assert not result.content and "empty" not in (result.hint or "")
    allowed = Path(live_env.workspace_root) / "allowed.txt"
    allowed.write_text("one\ntwo\n", encoding="utf-8")
    result = ops.read_file(str(allowed))
    assert result.error is None and result.content == "1|one\n2|two"
    missing = ops.read_file(str(allowed.parent / "missing.txt"))
    assert "File not found" in missing.error


def test_public_read_file_dispatch_preserves_real_sandbox_refusal(live_env, tmp_path):
    import model_tools
    from hermes_constants import get_hermes_home
    from tools.terminal_tool import cleanup_vm
    config = get_hermes_home() / "config.yaml"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["terminal"]["cwd"] = live_env.workspace_root
    config.write_text(json.dumps(data), encoding="utf-8")
    outside = tmp_path / "public-refused.txt"
    outside.write_text("REFUSED_PUBLIC_FIXTURE\n", encoding="utf-8")
    allowed = Path(live_env.workspace_root) / "public-allowed.txt"
    allowed.write_text("ALLOWED_PUBLIC_FIXTURE\n", encoding="utf-8")
    task = "mxc-public-read-fixture"
    try:
        result = json.loads(model_tools.handle_function_call("read_file", {"path": str(outside)}, task_id=task))
        assert result.get("error") and "refused" in result["error"], result
        assert "REFUSED_PUBLIC_FIXTURE" not in result.get("content", "")
        result = json.loads(model_tools.handle_function_call("read_file", {"path": str(allowed)}, task_id=task))
        assert not result.get("error") and "ALLOWED_PUBLIC_FIXTURE" in result["content"], result
    finally:
        cleanup_vm(task)


def test_command_state_and_absolute_file_tools(live_env):
    assert live_env.execute("export MXC_T=persisted; mkdir -p sub && cd sub")["returncode"] == 0
    result = live_env.execute("echo $MXC_T; pwd -P")
    assert "persisted" in result["output"] and result["output"].strip().endswith("/sub")
    assert result["sandbox"]["backend"] == "mxc"
    ops = ShellFileOperations(live_env)
    target = os.path.join(live_env.workspace_root, "note.txt")
    assert ops.write_file(target, "hello sandbox\n").error is None
    assert "hello sandbox" in ops.read_file(target).content
    assert ops.search("hello", path=live_env.workspace_root).total_count == 1


def test_last_grants_revoke_inside_bound_scope_despite_stale_bridge(live_env, tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    from tools.terminal_scope import install_and_reset_profile_terminal_scope
    config = get_hermes_home() / "config.yaml"
    extra = tmp_path / "extra"
    extra.mkdir()
    witness = extra / "witness.txt"
    witness.write_text("REVOCATION_FIXTURE\n", encoding="utf-8")
    monkeypatch.setenv("TERMINAL_MXC_READWRITE_PATHS", json.dumps([str(extra)]))
    monkeypatch.setenv("TERMINAL_MXC_READONLY_PATHS", json.dumps([str(witness)]))
    data = json.loads(config.read_text(encoding="utf-8"))
    data["terminal"].update(mxc_readwrite_paths=[str(extra)], mxc_readonly_paths=[str(witness)])
    config.write_text(json.dumps(data), encoding="utf-8")
    with install_and_reset_profile_terminal_scope(get_hermes_home()):
        before = ShellFileOperations(live_env).read_file(str(witness))
        assert before.error is None and "REVOCATION_FIXTURE" in before.content
        data["terminal"].update(mxc_readwrite_paths=[], mxc_readonly_paths=[])
        config.write_text(json.dumps(data), encoding="utf-8")
        assert mxc_host.resolve_settings().policy == mxc_host.MxcPolicy()
        after = ShellFileOperations(live_env).read_file(str(witness))
        assert after.error and "refused" in after.error
        write = live_env.execute(f"printf changed > '{witness.as_posix()}'")
        assert write["returncode"] != 0
        assert witness.read_text(encoding="utf-8") == "REVOCATION_FIXTURE\n"


def test_peer_workspace_and_executable_scratch_are_denied(live_env, tmp_path):
    from tools.environments.mxc import MxcEnvironment
    workspace = tmp_path / "peer"
    workspace.mkdir()
    peer = MxcEnvironment(cwd=str(workspace), timeout=30)
    try:
        assert peer._snapshot_ready
        peer_data = Path(peer.get_temp_dir()) / "peer.txt"
        peer_data.write_text("PEER_PRIVATE_FIXTURE\n", encoding="utf-8")
        peer_workspace = workspace / "peer.txt"
        peer_workspace.write_text("PEER_WORKSPACE_FIXTURE\n", encoding="utf-8")
        from hermes_constants import get_hermes_home
        legacy = get_hermes_home() / "cache" / "terminal" / "local-snapshot.sh"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("LOCAL_SNAPSHOT_FIXTURE_ONLY\n", encoding="utf-8")
        for target in (peer_data, peer_workspace, Path(peer._snapshot_path), legacy):
            original = target.read_bytes()
            result = ShellFileOperations(live_env).read_file(str(target))
            assert result.error and "refused" in result.error
            write = live_env.execute(f"printf forbidden > '{target.as_posix()}'")
            assert write["returncode"] != 0 and target.read_bytes() == original
    finally:
        private = Path(peer.get_temp_dir())
        peer.cleanup()
        assert not private.exists()


def test_cleanup_reaps_started_launcher_tree(live_env):
    import psutil
    proc = live_env._spawn_container("sleep 30", stdin=False)
    descendants = []
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            descendants = psutil.Process(proc.pid).children(recursive=True)
            if descendants:
                break
            time.sleep(0.05)
        assert descendants, "the fixture must actually start a sandboxed child"
        private = Path(live_env.get_temp_dir())
        live_env.cleanup()
        assert proc.poll() is not None, "cleanup must stop its launcher, not just unlink scripts"
        _, alive = psutil.wait_procs(descendants, timeout=5)
        assert not alive, [p.pid for p in alive]
        assert not private.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)
        for child in descendants:
            if child.is_running():
                child.kill()


def test_timeout_reaps_observed_children(live_env):
    import psutil
    proc = live_env._spawn_container("sleep 30", stdin=False)
    children = []
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            children = psutil.Process(proc.pid).children(recursive=True)
            if children:
                break
            time.sleep(0.05)
        assert children
        result = live_env._wait_for_process(proc, timeout=1)
        assert result["returncode"] == 124
        _, alive = psutil.wait_procs(children, timeout=5)
        assert not alive
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)
        for child in children:
            if child.is_running():
                child.kill()


def test_projectless_environment_uses_safe_default_for_real_reads(live_env, tmp_path, monkeypatch):
    from tools.environments.mxc import MxcEnvironment
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(user) if p == "~" else p)
    install = Path(mxc_host.__file__).resolve().parents[2]
    env = MxcEnvironment(cwd=str(install), timeout=30)
    try:
        assert Path(env.workspace_root) == user / mxc_host.DEFAULT_WORKSPACE_DIRNAME
        target = Path(env.workspace_root) / "default.txt"
        ops = ShellFileOperations(env)
        assert ops.write_file(str(target), "default workspace fixture\n").error is None
        result = ops.read_file(str(target))
        assert result.error is None and "default workspace fixture" in result.content
    finally:
        env.cleanup()


def test_actual_kit_dry_run_accepts_complete_strict_request(live_env):
    import base64
    script = live_env._write_script("true")
    try:
        request, launcher, _ = live_env.container_request(script)
        assert any(value.startswith("LOCALAPPDATA=") for value in request["process"]["env"])
        assert request["fallback"]["allowDaclMutation"] is False
        payload = base64.b64encode(json.dumps(request).encode("utf-8")).decode("ascii")
        result = subprocess.run([launcher, "--config-base64", payload, "--dry-run"],
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        live_env._discard_script(script)


def test_readonly_grants_and_attachment_scope(live_env, tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    extra = tmp_path / "readonly"
    extra.mkdir()
    witness = extra / "witness.txt"
    witness.write_text("READONLY_FIXTURE\n", encoding="utf-8")
    config = get_hermes_home() / "config.yaml"
    data = json.loads(config.read_text(encoding="utf-8"))
    data["terminal"]["mxc_readonly_paths"] = [str(extra)]
    config.write_text(json.dumps(data), encoding="utf-8")
    assert "READONLY_FIXTURE" in ShellFileOperations(live_env).read_file(str(witness)).content
    result = live_env.execute(f"printf forbidden > '{witness.as_posix()}'")
    assert result["returncode"] != 0 and witness.read_text(encoding="utf-8") == "READONLY_FIXTURE\n"
    user_data = tmp_path / "desktop"
    staging = user_data / "composer-pastes"
    staging.mkdir(parents=True)
    paste = staging / "paste.txt"
    paste.write_text("PASTE_FIXTURE\n", encoding="utf-8")
    tokens = user_data / "connections.json"
    tokens.write_text('{"fake_token":"FIXTURE_ONLY"}', encoding="utf-8")
    monkeypatch.setenv(mxc_host.DESKTOP_USER_DATA_ENV, str(user_data))
    ops = ShellFileOperations(live_env)
    assert "PASTE_FIXTURE" in ops.read_file(str(paste)).content
    refused = ops.read_file(str(tokens))
    assert refused.error and "refused" in refused.error
