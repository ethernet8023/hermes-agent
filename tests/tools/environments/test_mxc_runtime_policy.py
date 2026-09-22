"""Runtime boundary regressions using disposable policy and filesystem fixtures."""
import json
import os
from pathlib import Path

import pytest

from tools.environments import mxc_host


def test_explicit_empty_policy_revokes_stale_environment_grants(monkeypatch):
    monkeypatch.setenv("TERMINAL_MXC_READWRITE_PATHS", '["C:/revoked-rw"]')
    monkeypatch.setenv("TERMINAL_MXC_READONLY_PATHS", '["C:/revoked-ro"]')
    monkeypatch.setenv("TERMINAL_MXC_NETWORK", "true")
    settings = mxc_host.resolve_settings({
        "mxc_readwrite_paths": [], "mxc_readonly_paths": [], "mxc_network": False,
    })
    assert settings.policy == mxc_host.MxcPolicy()
    assert mxc_host.resolve_settings({}).policy == mxc_host.MxcPolicy()


def test_live_settings_and_backend_share_strict_authority(monkeypatch):
    from tools import terminal_scope
    def unavailable():
        raise terminal_scope.TerminalPolicyUnavailable("fixture policy unreadable")
    monkeypatch.setattr(terminal_scope, "get_live_terminal_config", unavailable, raising=False)
    for read in (mxc_host.resolve_settings, mxc_host.backend_enabled):
        with pytest.raises(terminal_scope.TerminalPolicyUnavailable, match="fixture policy unreadable"):
            read()


@pytest.mark.parametrize("writable", [False, True])
def test_user_grants_reject_protected_descendants_and_aliases(tmp_path, monkeypatch, writable):
    protected = tmp_path / "protected"
    descendant = protected / "plugins"
    descendant.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(protected))
    candidates = [protected, descendant, protected.parent, Path(mxc_host.__file__).resolve().parent]
    if os.name == "nt":
        candidates.append(Path("\\\\?\\" + str(descendant)))
    for candidate in candidates:
        with pytest.raises(ValueError, match="protected|Hermes|program files"):
            mxc_host.validate_grant_paths([str(candidate)], writable=writable)
    allowed = tmp_path / "project"
    allowed.mkdir()
    assert mxc_host.validate_grant_paths([str(allowed), str(allowed / ".." / "project")], writable=writable) == [str(allowed.resolve())]
    for malformed in (["relative"], [str(tmp_path / "missing")], [None], "not-a-list", [""]):
        with pytest.raises(ValueError):
            mxc_host.validate_grant_paths(malformed, writable=writable)


@pytest.fixture
def isolated_environment(tmp_path, monkeypatch):
    from tools.environments import mxc
    monkeypatch.setattr(mxc, "_IS_WINDOWS", True)
    monkeypatch.setattr(mxc.MxcEnvironment, "init_session", lambda self: None)
    monkeypatch.setattr(mxc.MxcEnvironment, "_resolve_launcher", lambda self, settings: ("launcher", "shell"))
    monkeypatch.setattr(mxc.MxcEnvironment, "_tool_readonly_grants", lambda self, shell: [])
    monkeypatch.setattr(mxc.MxcEnvironment, "_git_identity_env", lambda self: {})
    made = []
    def create(name):
        workspace = tmp_path / name
        workspace.mkdir()
        env = mxc.MxcEnvironment(cwd=str(workspace), settings=mxc_host.resolve_settings({}))
        made.append(env)
        return env
    yield create
    for env in made:
        env.cleanup()


def test_scratch_grants_are_private_and_removed_at_cleanup(isolated_environment):
    a, b = isolated_environment("a"), isolated_environment("b")
    assert a.get_temp_dir() != b.get_temp_dir()
    script = a._write_script("true")
    request, _, _ = a.container_request(script)
    grants = [Path(p) for p in request["filesystem"]["readwritePaths"]]
    assert Path(a._snapshot_path).parent == Path(a.get_temp_dir())
    assert not any(Path(b._snapshot_path).is_relative_to(root) for root in grants)
    assert not any(Path(b.get_temp_dir()).is_relative_to(root) for root in grants)
    private = Path(a.get_temp_dir())
    a.cleanup()
    assert not private.exists()
    assert Path(b.get_temp_dir()).is_dir()


def test_final_request_revalidates_user_authority(isolated_environment, tmp_path):
    from hermes_constants import get_hermes_home
    env = isolated_environment("project")
    protected = get_hermes_home() / "plugins"
    protected.mkdir()
    settings = mxc_host.MxcSettings(None, None, mxc_host.MxcPolicy(readonly_paths=(str(protected),)))
    with pytest.raises(ValueError, match="data directory"):
        env.container_request("script", settings=settings)
    assert "data directory" in mxc_host.unsafe_workspace_reason(str(protected))
    with pytest.raises(ValueError, match="data directory"):
        mxc_host.resolve_settings({"mxc_readwrite_paths": [str(protected)]})
    for key, value in (("mxc_network", "perhaps"), ("mxc_readonly_paths", None), ("mxc_debug", [])):
        with pytest.raises(ValueError):
            mxc_host.resolve_settings({key: value})


_UI_CAPABILITIES = (
    "canBlockClipboardRead", "canBlockClipboardWrite", "canBlockInputInjection",
    "canBlockInputMethodChanges", "canBlockExternalUiObjects", "canBlockGlobalUiNamespace",
    "canBlockDesktopSwitching", "canBlockLogoffOrShutdown",
    "canBlockSystemParameterChanges", "canBlockDisplaySettingsChanges",
)


@pytest.mark.parametrize("missing", ["tier", "baseContainerApiPresent", *_UI_CAPABILITIES])
def test_status_and_launcher_refuse_weaker_containment(monkeypatch, missing):
    from tools.environments.mxc import MxcEnvironment
    probe = {"ok": True, "tier": "base-container", "warnings": [], "error": None,
             "probes": {"baseContainerApiPresent": True,
                        "uiCapabilities": dict.fromkeys(_UI_CAPABILITIES, True)}}
    if missing == "tier":
        probe["tier"] = "appcontainer-dacl"
    elif missing == "baseContainerApiPresent":
        probe["probes"][missing] = False
    else:
        probe["probes"]["uiCapabilities"][missing] = False
    monkeypatch.setattr(mxc_host, "_IS_WINDOWS", True)
    monkeypatch.setattr(mxc_host, "find_wxc_exec", lambda configured=None: "launcher")
    monkeypatch.setattr(mxc_host, "run_probe", lambda path: probe)
    monkeypatch.setattr(mxc_host, "ensure_shell", lambda *a, **kw: ("shell", None))
    settings = mxc_host.resolve_settings({})
    record = mxc_host.status(settings=settings)
    assert not record["available"] and record["reason"]
    with pytest.raises(RuntimeError):
        MxcEnvironment.__new__(MxcEnvironment)._resolve_launcher(settings)


def test_every_request_disables_dacl_mutation():
    from tools.environments.mxc import build_container_config
    request = build_container_config(container_id="test", command_line="true", cwd=os.getcwd(),
                                     env={}, readwrite_paths=[], readonly_paths=[], network=False)
    assert request.get("fallback", {}).get("allowDaclMutation") is False


def test_retired_ancestor_preparation_never_claims_ready_or_mutates(monkeypatch):
    calls = []
    def no_process(*args, **kwargs):
        calls.append(args)
        raise OSError("fixture ACL unreadable")
    monkeypatch.setattr(mxc_host.subprocess, "run", no_process)
    readiness = mxc_host.ancestor_readiness(os.getcwd())
    assert readiness["ready"] is False and readiness["error"]
    with pytest.raises(RuntimeError, match="disabled"):
        mxc_host.prepare_ancestors(os.getcwd())
    assert calls == []
    from tools.environments.mxc import denial_note, GIT_ANCESTOR_DENIAL
    note = denial_note([GIT_ANCESTOR_DENIAL], workspace="project", policy=mxc_host.MxcPolicy())
    assert "Prepare workspace" not in note and "icacls" not in note


def test_status_reports_invalid_policy_as_unavailable(monkeypatch):
    from tools import terminal_scope
    def unavailable():
        raise terminal_scope.TerminalPolicyUnavailable("fixture invalid YAML")
    monkeypatch.setattr(terminal_scope, "get_live_terminal_config", unavailable, raising=False)
    record = mxc_host.status()
    assert record["available"] is False and "fixture invalid YAML" in record["reason"]


def test_internal_shell_grant_does_not_expose_its_parent(tmp_path):
    from tools.environments.mxc import MxcEnvironment
    parent = tmp_path / "tools"
    parent.mkdir()
    shell = parent / "busybox-sh.exe"
    shell.write_bytes(b"fixture")
    secret = parent / "private.txt"
    secret.write_text("FAKE_TOKEN_ONLY", encoding="utf-8")
    env = MxcEnvironment.__new__(MxcEnvironment)
    grants = [Path(p) for p in env._tool_readonly_grants(str(shell))]
    assert shell in grants
    assert not any(secret.is_relative_to(root) for root in grants)


@pytest.mark.windows_only
def test_reparse_roots_cannot_turn_internal_exceptions_into_protected_grants(tmp_path, monkeypatch):
    import subprocess
    from hermes_constants import get_hermes_home
    from tools.environments.mxc import MxcEnvironment
    home = get_hermes_home()
    protected = home / "plugins"
    protected.mkdir()
    (tmp_path / "shell.exe").write_bytes(b"fixture")
    user_home = tmp_path / "user"
    user_home.mkdir()
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(user_home) if p == "~" else p)
    for alias in (home / "bin", user_home / mxc_host.DEFAULT_WORKSPACE_DIRNAME):
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(protected)],
                              capture_output=True, text=True, timeout=10)
        assert made.returncode == 0, made.stderr
        try:
            for writable in (False, True):
                with pytest.raises(ValueError, match="data directory"):
                    mxc_host.validate_grant_paths([str(alias)], writable=writable)
            if alias.name == "bin":
                with pytest.raises(ValueError, match="alias|protected"):
                    MxcEnvironment.__new__(MxcEnvironment)._tool_readonly_grants(str(tmp_path / "shell.exe"))
            else:
                with pytest.raises(ValueError, match="data directory"):
                    mxc_host.sandbox_workspace_for(str(user_home))
        finally:
            os.rmdir(alias)


@pytest.mark.windows_only
def test_host_script_creation_refuses_replaced_scratch_root(isolated_environment, tmp_path):
    import subprocess
    env = isolated_environment("workspace")
    scratch = Path(env.get_temp_dir())
    saved = scratch.with_name(scratch.name + "-saved")
    target = tmp_path / "ungranted"
    target.mkdir()
    scratch.rename(saved)
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(scratch), str(target)],
                          capture_output=True, text=True, timeout=10)
    assert made.returncode == 0, made.stderr
    try:
        with pytest.raises(RuntimeError, match="scratch"):
            env._write_script("fixture")
        assert list(target.iterdir()) == []
    finally:
        os.rmdir(scratch)
        saved.rename(scratch)


def test_custom_profiles_still_protect_native_data_and_shell_exceptions(tmp_path, monkeypatch):
    import hermes_constants
    from tools.environments.mxc import MxcEnvironment
    native = tmp_path / "native"
    native.mkdir()
    secret = native / "auth.json"
    secret.write_text('{"fake":"TOKEN_FIXTURE_ONLY"}', encoding="utf-8")
    monkeypatch.setattr(hermes_constants, "_get_platform_default_hermes_home", lambda: native)
    with pytest.raises(ValueError, match="data directory"):
        mxc_host.validate_grant_paths([str(secret)], writable=False)
    with pytest.raises(ValueError, match="data directory"):
        MxcEnvironment.__new__(MxcEnvironment)._tool_readonly_grants(str(secret))


def test_live_runtime_settings_follow_profile_a_b_a_without_bridge_leak(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.terminal_scope import install_and_reset_profile_terminal_scope
    extra = tmp_path / "extra"
    extra.mkdir()
    homes = [tmp_path / name for name in ("profile-a", "profile-b")]
    for home, paths in zip(homes, ([str(extra)], [])):
        home.mkdir()
        (home / "config.yaml").write_text(json.dumps({"terminal": {
            "backend": "mxc", "mxc_readwrite_paths": paths, "mxc_readonly_paths": [],
            "mxc_network": False,
        }}), encoding="utf-8")
    monkeypatch.setenv("TERMINAL_MXC_READWRITE_PATHS", json.dumps([str(extra)]))
    for home, expected in ((homes[0], (str(extra.resolve()),)), (homes[1], ()),
                           (homes[0], (str(extra.resolve()),))):
        token = set_hermes_home_override(home)
        try:
            with install_and_reset_profile_terminal_scope(home):
                assert mxc_host.backend_enabled()
                assert mxc_host.resolve_settings().policy.readwrite_paths == expected
        finally:
            reset_hermes_home_override(token)
