"""Live contract of the Windows MXC backend against the real ``wxc-exec`` (skips when the MXC kit is
absent). Proves the properties the sandbox story rests on: commands run, state persists across
containers, out-of-policy writes and reads are refused by the OS, network is off by default, and
timeouts kill the container.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools.environments import mxc_host

pytestmark = pytest.mark.platforms("windows")


def _provisioned_shell() -> str | None:
    """The sandbox shell in the pm store; tests never download it."""
    binary = mxc_host.store_binary("busybox")
    return str(binary) if binary is not None else None


@pytest.fixture
def live_env(monkeypatch):
    shell = _provisioned_shell()
    if shell is None:
        pytest.skip("sandbox shell not provisioned on this machine (enable the sandbox once first)")
    record = mxc_host.status(provision_shell=False)
    if not record["available"]:
        pytest.skip(f"MXC unavailable here: {record['reason']}")
    from tools.environments.mxc import MxcEnvironment
    # A top-level workspace keeps the test independent of ancestor ACLs (see mxc_host.workspace_ancestors).
    drive = os.path.splitdrive(os.getcwd())[0] + os.sep
    workspace = os.path.join(drive, "hermes-mxc-test-" + os.urandom(3).hex())
    os.makedirs(workspace, exist_ok=True)
    env = MxcEnvironment(cwd=workspace, timeout=60)
    try:
        yield env
    finally:
        env.cleanup()
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", workspace], capture_output=True)


def test_command_runs_and_reports_its_container(live_env):
    result = live_env.execute("echo hello-from-mxc")
    assert result["returncode"] == 0 and "hello-from-mxc" in result["output"]
    assert result["sandbox"]["backend"] == "mxc" and result["sandbox"]["container"].startswith("hermes-")


def test_exports_and_cwd_persist_across_containers(live_env):
    assert live_env.execute("export MXC_T=persisted; mkdir -p sub && cd sub")["returncode"] == 0
    result = live_env.execute("echo $MXC_T; pwd -P")
    assert "persisted" in result["output"] and result["output"].strip().endswith("/sub")


def test_out_of_policy_write_is_refused_by_the_os(live_env):
    target = os.path.join(os.path.expanduser("~"), "hermes-mxc-should-not-exist.txt")
    result = live_env.execute(f"echo nope > '{target.replace(os.sep, '/')}'")
    assert not os.path.exists(target)
    assert "Permission denied" in result["output"] and "[Sandbox]" in result["output"]
    assert result["sandbox"]["denied"]


def test_hermes_credentials_are_unreadable_from_the_sandbox(live_env):
    from hermes_constants import get_hermes_home
    env_file = (get_hermes_home() / ".env").as_posix()
    result = live_env.execute(f"cat '{env_file}' >/dev/null 2>&1; echo rc=$?")
    assert "rc=1" in result["output"]


def test_network_is_off_by_default(live_env):
    result = live_env.execute("curl.exe -sS -m 5 -o NUL https://example.com; echo rc=$?")
    assert "rc=0" not in result["output"]


def test_timeout_kills_the_container(live_env):
    result = live_env.execute("sleep 30", timeout=2)
    assert result["returncode"] == 124


def test_composer_attachments_are_readable_but_the_rest_of_user_data_is_not(live_env, tmp_path, monkeypatch):
    user_data = tmp_path / "user-data"
    (user_data / "composer-images").mkdir(parents=True)
    (user_data / "composer-images" / "shot.png").write_bytes(b"PNG")
    (user_data / "connections.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(mxc_host.DESKTOP_USER_DATA_ENV, str(user_data))
    image = (user_data / "composer-images" / "shot.png").as_posix()
    tokens = (user_data / "connections.json").as_posix()
    result = live_env.execute(f"cat '{image}'; echo; cat '{tokens}' >/dev/null 2>&1; echo rc=$?")
    assert "PNG" in result["output"] and "rc=1" in result["output"]


def test_search_files_outside_the_policy_reports_the_refusal_not_a_missing_path(live_env):
    """The file tools route through the container; a folder the OS refuses to stat exists, so the
    search must say it was refused (with the policy note), never "Path not found"."""
    from tools.file_operations import ShellFileOperations
    ops = ShellFileOperations(live_env)
    result = ops.search("*", path=os.path.expanduser("~"), target="files")
    assert result.error is not None
    assert "refused" in result.error and "[Sandbox]" in result.error
    assert "Path not found" not in result.error


def test_file_tools_take_absolute_windows_paths_and_report_refusals(live_env):
    """Absolute ``C:\\...`` paths are the model's default spelling; they must reach the sandbox shell
    in its own dialect, and a refused read must not masquerade as a missing file."""
    from tools.file_operations import ShellFileOperations
    ops = ShellFileOperations(live_env)
    target = os.path.join(live_env.workspace_root, "note.txt")
    assert ops.write_file(target, "hello sandbox\n").error is None
    read = ops.read_file(target)
    assert read.error is None and "hello sandbox" in read.content
    assert ops.search("hello", path=live_env.workspace_root).total_count == 1
    refused = ops.read_file(os.path.join(os.path.expanduser("~"), "NTUSER.DAT"))
    assert refused.error is not None and "refused" in refused.error and "File not found" not in refused.error


def test_a_refused_workspace_rehomes_to_the_default_instead_of_failing(monkeypatch, tmp_path):
    """The sandbox turned on for a session whose cwd is the install tree (or empty, which means the
    backend's own directory) must not take every tool down: the environment works in the default
    workspace and says so, and a command there succeeds."""
    shell = _provisioned_shell()
    if shell is None:
        pytest.skip("sandbox shell not provisioned on this machine (enable the sandbox once first)")
    if not mxc_host.status(provision_shell=False)["available"]:
        pytest.skip("MXC unavailable here")
    from tools.environments.mxc import MxcEnvironment
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)
    install = str(Path(mxc_host.__file__).resolve().parents[2])
    env = MxcEnvironment(cwd=install, timeout=60)
    try:
        expected = str(home / mxc_host.DEFAULT_WORKSPACE_DIRNAME)
        assert os.path.normcase(env.workspace_root) == os.path.normcase(expected)
        result = env.execute("echo ok-from-default && pwd -P")
        assert result["returncode"] == 0 and "ok-from-default" in result["output"]
        assert mxc_host.DEFAULT_WORKSPACE_DIRNAME.lower() in result["output"].lower()
    finally:
        env.cleanup()
