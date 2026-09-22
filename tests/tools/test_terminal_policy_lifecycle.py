"""Live terminal authority and resource retirement, using disposable profiles."""
from contextlib import contextmanager

import pytest
import yaml

from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from tools import terminal_scope as scope


def policy(home, **terminal):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"terminal": terminal}), encoding="utf-8")


@contextmanager
def bound(home, **kwargs):
    ht = set_hermes_home_override(home)
    tt = scope.install_profile_terminal_scope(home, **kwargs)
    try:
        yield
    finally:
        scope.reset_terminal_scope(tt)
        reset_hermes_home_override(ht)


def test_bound_turn_observes_policy_replacement(tmp_path, monkeypatch):
    from tools.terminal_tool import _get_env_config
    monkeypatch.setenv("TERMINAL_ENV", "local")
    home = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy(home, backend="local", cwd=str(workspace))
    with bound(home):
        assert _get_env_config()["env_type"] == "local"
        policy(home, backend="mxc", cwd=str(workspace), mxc_readwrite_paths=[], mxc_network=False)
        assert _get_env_config()["env_type"] == "mxc"
        live = scope.get_live_terminal_config()
        assert live["backend"] == "mxc"
        assert live["mxc_readwrite_paths"] == []
        assert live["mxc_network"] is False
        assert live["mxc_debug"] is False
        policy(home, backend="local", cwd=str(workspace))
        assert _get_env_config()["env_type"] == "local"


@pytest.mark.parametrize("text", ["[]", "terminal: []", "terminal: null", "terminal:\n  backend: []", "terminal:\n  backend: mxc\n  mxc_network: perhaps", "terminal:\n  backend: mxc\n  mxc_readwrite_paths: nope"])
def test_invalid_live_policy_refuses_instead_of_defaulting(tmp_path, text):
    home = tmp_path / "profile"
    policy(home, backend="local")
    with bound(home):
        (home / "config.yaml").write_text(text, encoding="utf-8")
        with pytest.raises(scope.TerminalPolicyUnavailable):
            scope.get_live_terminal_config()


def test_launch_overlay_and_secondary_empty_grants_are_independent(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    policy(a, mxc_readwrite_paths=["C:/launch-grant"])
    policy(b, backend="local", mxc_readwrite_paths=[])
    monkeypatch.setenv("TERMINAL_MXC_READWRITE_PATHS", '["C:/ambient"]')
    for home, backend, grants in [(a, "mxc", ["C:/launch-grant"]), (b, "local", []), (a, "mxc", [])]:
        with bound(home, env_overlay={"TERMINAL_ENV": "mxc"} if home == a else None):
            assert scope.get_live_terminal_config()["backend"] == backend
            assert scope.get_live_terminal_config()["mxc_readwrite_paths"] == grants
            if home == a:
                policy(a, mxc_readwrite_paths=[])


class Environment:
    def __init__(self, backend, cwd):
        self.env_type, self.cwd = backend, cwd
        self.cleaned = False
        self.executed = []

    def cleanup(self):
        self.cleaned = True

    def execute(self, command, **kwargs):
        self.executed.append(command)
        return {"output": command, "returncode": 0}


@pytest.fixture
def environments(monkeypatch):
    from tools import terminal_tool as terminal
    monkeypatch.setattr(terminal, "_active_environments", {})
    monkeypatch.setattr(terminal, "_last_activity", {})
    monkeypatch.setattr(terminal, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal, "_resolve_container_task_id", lambda task: task)
    monkeypatch.setattr(terminal, "_create_configured_env", lambda config, backend, **kw: Environment(backend, kw["cwd"]))
    return terminal


def acquire(terminal, task):
    plan = terminal._plan_execution("true", task_id=task, timeout=None, background=False, _host_local=False)
    return terminal._acquire_env(plan, task)


def test_acquisition_reconciles_external_edit_only_for_owning_profile(tmp_path, environments):
    t = environments
    a, b = tmp_path / "a", tmp_path / "b"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for home in (a, b):
        policy(home, backend="local", cwd=str(workspace))
    with bound(a):
        old_a = acquire(t, "a")
    with bound(b):
        old_b = acquire(t, "b")
    with bound(a):
        policy(a, backend="mxc", cwd=str(workspace))
        new_a = acquire(t, "a")
        assert new_a.env_type == "mxc"
        assert old_a.cleaned
        assert not old_b.cleaned
        assert t._active_environments["b"] is old_b


def test_retained_environment_cannot_execute_after_policy_change(tmp_path, environments):
    home = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy(home, backend="local", cwd=str(workspace))
    with bound(home):
        env = acquire(environments, "a")
        policy(home, backend="mxc", cwd=str(workspace))
        with pytest.raises(scope.TerminalPolicyUnavailable):
            env.execute("must not execute")
        assert not env.executed


def test_file_cache_uses_same_generation_as_terminal(tmp_path, environments, monkeypatch):
    from tools import file_tools
    monkeypatch.setattr(file_tools, "_file_ops_cache", {})
    home = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy(home, backend="local", cwd=str(workspace))
    with bound(home):
        old = file_tools._get_file_ops("a")
        policy(home, backend="mxc", cwd=str(workspace))
        new = file_tools._get_file_ops("a")
        assert new is not old
        assert new.env.env_type == "mxc"
        assert old.env.cleaned


def test_code_execution_acquires_the_same_current_environment(tmp_path, environments):
    from tools.code_execution_tool import _get_or_create_env
    home = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy(home, backend="local", cwd=str(workspace))
    with bound(home):
        old = acquire(environments, "a")
        policy(home, backend="mxc", cwd=str(workspace))
        current, backend = _get_or_create_env("a")
        assert current is not old and old.cleaned
        assert current.env_type == backend == "mxc"


def test_refused_host_action_still_reconciles_prior_execution(tmp_path, environments):
    from tools.environments.mxc_policy import tool_refusal
    home = tmp_path / "profile"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy(home, backend="local", cwd=str(workspace))
    with bound(home):
        old = acquire(environments, "a")
        policy(home, backend="mxc", cwd=str(workspace))
        assert tool_refusal("browser_exec", {}) is not None
        assert old.cleaned


def test_reconcile_retires_owned_kernels_and_processes(tmp_path, environments, monkeypatch):
    from tools import code_kernel, process_registry as processes
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setattr(code_kernel._REGISTRY, "kernels", {})
    monkeypatch.setattr(code_kernel, "_KERNELS", code_kernel._REGISTRY.kernels)
    monkeypatch.setattr(processes.process_registry, "_running", {})
    killed = []
    def kill(sid, **kwargs):
        killed.append(sid)
        processes.process_registry._running[sid].exited = True
        return {"status": "killed"}
    monkeypatch.setattr(processes.process_registry, "kill_process", kill)
    kernels, jobs = [], []
    for home in (a, b):
        policy(home, backend="local")
        with bound(home):
            reconcile_terminal_policy()
            k = code_kernel.SessionKernel((str(home),))
            code_kernel._KERNELS[k.key] = k
            kernels.append(k)
            job = processes.process_registry._new_session("test", "task", "task", "session", str(tmp_path))
            processes.process_registry._running[job.id] = job
            jobs.append(job)
    with bound(a):
        policy(a, backend="mxc")
        reconcile_terminal_policy()
    assert kernels[0].stop_event.is_set()
    assert not kernels[1].stop_event.is_set()
    assert killed == [jobs[0].id]


def test_retired_pending_kernel_never_spawns(tmp_path, monkeypatch):
    from tools import code_kernel
    policy(tmp_path / "profile", backend="local")
    spawned = []
    monkeypatch.setattr(code_kernel, "_spawn", lambda *a, **kw: spawned.append(True))
    with bound(tmp_path / "profile"):
        kernel = code_kernel.SessionKernel(("owner",))
        kernel.teardown()
        code_kernel._run_cell(kernel, kernel.key, "pass", task_id="task", child_python="python",
            child_cwd=str(tmp_path), sandbox_tools=frozenset(), timeout=1, max_tool_calls=1,
            is_interrupted=lambda: False, exec_start=0, state_reset=False)
    assert spawned == []


def test_mxc_process_controller_refuses_host_and_foreign_targets(tmp_path, monkeypatch):
    import json
    from hermes_constants import hermes_home_key
    from tools import process_registry as p
    home = tmp_path / "profile"
    policy(home, backend="mxc")
    registry = p.process_registry
    monkeypatch.setattr(registry, "_running", {})
    monkeypatch.setattr(registry, "_finished", {})
    called = []
    monkeypatch.setitem(p._SESSION_ACTIONS, "submit", (lambda sid, args: called.append(sid) or {"status": "sent"}, False))
    with bound(home):
        from tools.terminal_policy_lifecycle import reconcile_terminal_policy
        reconcile_terminal_policy()
        for sid, owner, backend in [("proc_host", hermes_home_key(), "local"), ("proc_foreign", "other", "mxc"), ("proc_owned", hermes_home_key(), "mxc")]:
            registry._running[sid] = p.ProcessSession(id=sid, command="test", profile_home=owner, terminal_backend=backend, owner_task_id="caller")
        for sid in ("proc_host", "proc_foreign"):
            assert "error" in json.loads(p._handle_process({"action": "submit", "session_id": sid, "data": "x"}))
        assert json.loads(p._handle_process({"action": "submit", "session_id": "proc_owned", "data": "x"}, task_id="caller"))["status"] == "sent"
        refreshed = []
        monkeypatch.setattr(registry, "_refresh_detached_session", lambda s: refreshed.append(s.id) or s)
        listing = json.loads(p._handle_process({"action": "list"}, task_id="caller"))["processes"]
        assert [p["session_id"] for p in listing] == ["proc_owned"]
        assert refreshed == ["proc_owned"]
    assert called == ["proc_owned"]


def test_background_spawn_owns_profile_and_execution_backend(tmp_path, monkeypatch):
    from tools import terminal_tool_background as background, process_registry as p
    from hermes_constants import hermes_home_key
    from types import SimpleNamespace
    home = tmp_path / "profile"
    policy(home, backend="mxc")
    monkeypatch.setattr(p.process_registry, "adopt_local", lambda proc, **kw: p.process_registry._new_session(kw["command"], kw["task_id"], kw["owner_task_id"], kw["session_key"], kw["cwd"]))
    with bound(home):
        env = SimpleNamespace(env_type="mxc", spawn_background_process=lambda *a: object())
        result = background._spawn(p.process_registry, env=env, env_type="mxc", command="test", cwd=str(tmp_path), effective_task_id="t", task_id="t", session_key="s", effective_pty=False)
        assert result.profile_home == hermes_home_key()
        assert result.terminal_backend == "mxc"


def test_mxc_plan_rehomes_raw_override_before_execution(tmp_path, environments, monkeypatch):
    from tools.environments import mxc_host
    home, safe = tmp_path / "profile", tmp_path / "safe"
    safe.mkdir()
    policy(home, backend="mxc", cwd=str(safe))
    monkeypatch.setattr(mxc_host, "sandbox_workspace_for", lambda cwd: str(safe))
    monkeypatch.setattr(environments, "resolve_task_overrides", lambda task: {"cwd": str(home)})
    with bound(home):
        plan = environments._plan_execution("test", task_id="t", timeout=None, background=False, _host_local=False)
        assert plan.cwd == str(safe)


def test_workspace_backend_uses_bound_profile(tmp_path, monkeypatch):
    from tui_gateway import server
    home = tmp_path / "profile"
    monkeypatch.setenv("TERMINAL_ENV", "mxc")
    policy(home, backend="local")
    with bound(home):
        assert server._effective_terminal_backend() == "local"


@pytest.mark.parametrize("writer", ["save", "atomic"])
def test_generic_config_writers_reconcile_active_profile(tmp_path, environments, writer):
    from hermes_cli.config import save_config, atomic_config_write
    home, workspace = tmp_path / "profile", tmp_path / "workspace"
    workspace.mkdir()
    policy(home, backend="local", cwd=str(workspace))
    with bound(home):
        env = acquire(environments, "t")
        config = {"terminal": {"backend": "mxc", "cwd": str(workspace)}}
        if writer == "save":
            save_config(config)
        else:
            atomic_config_write(home / "config.yaml", config)
        assert env.cleaned
        assert scope.get_live_terminal_config()["backend"] == "mxc"


def test_recorded_readonly_cwd_never_becomes_recreated_workspace(tmp_path, environments, monkeypatch):
    home, a, b = tmp_path / "profile", tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    policy(home, backend="mxc", cwd=str(a), mxc_readonly_paths=[str(b)])
    monkeypatch.setattr(environments, "resolve_task_overrides", lambda task: {})
    with bound(home):
        original = acquire(environments, "cwd-test")
        environments.record_session_cwd("cwd-test", str(b))
        environments._active_environments.pop("cwd-test")
        recreated = acquire(environments, "cwd-test")
        assert recreated.cwd == original.cwd == str(a)
        assert environments.get_session_cwd("cwd-test") == str(b)


def test_lazy_acquisition_rejects_stale_backend(tmp_path, environments):
    from tools.terminal_tool_lifecycle import ensure_task_env
    home, workspace = tmp_path / "profile", tmp_path / "workspace"
    workspace.mkdir()
    policy(home, backend="ssh", cwd=str(workspace))
    with bound(home):
        old = acquire(environments, "lazy")
        policy(home, backend="mxc", cwd=str(workspace))
        current = ensure_task_env("lazy")
        assert current.env_type == "mxc"
        assert old.cleaned


def test_live_kernel_retirement_revokes_cell_authority(tmp_path, environments, monkeypatch):
    import subprocess
    import sys
    from tools import code_kernel
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy
    home = tmp_path / "profile"
    policy(home, backend="local")
    monkeypatch.setattr(code_kernel._REGISTRY, "kernels", {})
    with bound(home):
        reconcile_terminal_policy()
        kernel = code_kernel.SessionKernel(("live",))
        kernel.cell_authority = code_kernel.CellAuthority("t")
        kernel.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=str(tmp_path), start_new_session=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        code_kernel._REGISTRY.kernels[kernel.key] = kernel
        try:
            assert kernel.proc.poll() is None
            policy(home, backend="mxc")
            reconcile_terminal_policy()
            assert kernel.proc.poll() is not None
            assert not kernel.cell_authority.active
        finally:
            if kernel.proc.poll() is None:
                kernel.proc.kill()
            kernel.proc.wait(timeout=10)


def test_remote_kernels_are_profile_owned_during_policy_tightening(tmp_path, environments, monkeypatch):
    from tools import code_kernel_remote as remote
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy
    home = tmp_path / "profile"
    policy(home, backend="ssh", mxc_network=True)
    monkeypatch.setattr(remote._REGISTRY, "kernels", {})
    env = Environment("ssh", str(tmp_path))
    def remote_execute(command, **kwargs):
        env.executed.append(command)
        return {"output": "", "returncode": 1 if command.startswith("kill -0") else 0}
    monkeypatch.setattr(env, "execute", remote_execute)
    with bound(home):
        reconcile_terminal_policy()
        kernel = remote.RemoteKernel(env, "ssh", "/tmp/kernel", "42", "test-token", "owner")
        remote._REGISTRY.kernels[("owner",)] = kernel
        policy(home, backend="mxc", mxc_network=False)
        reconcile_terminal_policy()
    assert any("kill" in command for command in env.executed)
    assert not remote._REGISTRY.kernels


def test_remote_retirement_failure_does_not_confirm_policy(tmp_path, environments, monkeypatch):
    from tools import code_kernel_remote as remote
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy
    home = tmp_path / "profile"
    policy(home, backend="ssh")
    monkeypatch.setattr(remote._REGISTRY, "kernels", {})
    env = Environment("ssh", str(tmp_path))
    monkeypatch.setattr(env, "execute", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("transport unavailable")))
    with bound(home):
        reconcile_terminal_policy()
        kernel = remote.RemoteKernel(env, "ssh", "/tmp/kernel", "42", "token", "owner")
        remote._REGISTRY.kernels[("owner",)] = kernel
        policy(home, backend="mxc")
        with pytest.raises(Exception, match="transport unavailable"):
            reconcile_terminal_policy()
        assert remote._REGISTRY.kernels


def test_equivalent_control_plane_scope_does_not_retire_resources(tmp_path, environments):
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy
    home = tmp_path / "profile"
    policy(home, backend="local")
    with bound(home):
        env = acquire(environments, "same")
        token = scope.set_terminal_scope(None)
        try:
            reconcile_terminal_policy()
            assert not env.cleaned
        finally:
            scope.reset_terminal_scope(token)


def test_mxc_never_adopts_an_unowned_cached_host_environment(tmp_path, environments):
    home, work = tmp_path / "profile", tmp_path / "work"
    work.mkdir()
    policy(home, backend="mxc", cwd=str(work))
    environments._active_environments["unknown"] = Environment("local", str(work))
    with bound(home):
        from tools.terminal_policy_lifecycle import reconcile_terminal_policy
        with pytest.raises(scope.TerminalPolicyUnavailable):
            reconcile_terminal_policy()
        with pytest.raises(scope.TerminalPolicyUnavailable):
            acquire(environments, "unknown")


def test_constructor_policy_race_cannot_publish_old_environment(tmp_path, environments, monkeypatch):
    import contextvars
    import threading
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy
    home, work = tmp_path / "profile", tmp_path / "work"
    work.mkdir()
    policy(home, backend="local", cwd=str(work))
    entered, release = threading.Event(), threading.Event()
    created, failures = [], []
    def construct(config, backend, **kwargs):
        env = Environment(backend, kwargs["cwd"])
        created.append(env)
        entered.set()
        assert release.wait(5)
        return env
    monkeypatch.setattr(environments, "_create_configured_env", construct)
    def construct_call():
        try:
            acquire(environments, "raced")
        except scope.TerminalPolicyUnavailable as exc:
            failures.append(exc)
    with bound(home):
        thread = threading.Thread(target=contextvars.copy_context().run, args=(construct_call,))
        thread.start()
        assert entered.wait(5)
        policy(home, backend="mxc", cwd=str(work))
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        reconcile_terminal_policy()
        assert failures
        assert created[0].cleaned
        assert "raced" not in environments._active_environments


def test_explicit_workspace_selection_recreates_only_its_environment(tmp_path, environments, monkeypatch):
    home, a, b = tmp_path / "profile", tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    policy(home, backend="mxc", cwd=str(a))
    monkeypatch.setattr(environments, "_task_env_overrides", {})
    with bound(home):
        old = acquire(environments, "workspace")
        other = acquire(environments, "other")
        environments.register_task_env_overrides("workspace", {"cwd": str(b)})
        new = acquire(environments, "workspace")
        assert new is not old
        assert old.cleaned
        assert not other.cleaned
        assert new.cwd == str(b)


def test_equal_session_ids_do_not_share_kernels_across_profiles(tmp_path, monkeypatch):
    from tools import code_kernel, code_kernel_remote
    monkeypatch.setattr(code_kernel._REGISTRY, "kernels", {})
    monkeypatch.setattr(code_kernel, "_KERNELS", code_kernel._REGISTRY.kernels)
    monkeypatch.setattr(code_kernel, "_resolve_owner", lambda task: "same-session")
    monkeypatch.setattr(code_kernel, "_run_cell", lambda kernel, *a, **kw: kernel)
    kernels, remote_keys = [], []
    for name in ("a", "b"):
        home = tmp_path / name
        policy(home, backend="local")
        with bound(home):
            kernels.append(code_kernel.execute_in_session_kernel("pass", task_id="same", mode="local",
                child_python="python", child_cwd=str(tmp_path), sandbox_tools=frozenset(), timeout=1,
                max_tool_calls=1, reset=False, is_interrupted=lambda: False))
            remote_keys.append(code_kernel_remote._kernel_key("same-session", "ssh", "same", frozenset()))
    assert kernels[0] is not kernels[1]
    assert remote_keys[0] != remote_keys[1]


def test_remote_constructor_cannot_publish_after_policy_change(tmp_path, environments, monkeypatch):
    from tools import code_kernel_remote as remote
    home = tmp_path / "profile"
    policy(home, backend="ssh")
    monkeypatch.setattr(remote._REGISTRY, "kernels", {})
    monkeypatch.setattr(remote, "_REMOTE_KERNELS", remote._REGISTRY.kernels)
    env = Environment("ssh", str(tmp_path))
    monkeypatch.setattr(env, "execute", lambda cmd, **kw: {"output": "", "returncode": 1 if cmd.startswith("kill -0") else 0})
    def spawn(*args, **kwargs):
        policy(home, backend="mxc")
        return remote.RemoteKernel(env, "ssh", "/tmp/kernel", "42", "token", "owner")
    monkeypatch.setattr(remote, "_spawn_remote_kernel", spawn)
    with bound(home):
        with pytest.raises(scope.TerminalPolicyUnavailable):
            remote._acquire_remote_kernel(env, "ssh", "owner", "task", frozenset(), reset=False, idle_exit=60)
        assert not remote._REGISTRY.kernels


def test_process_invocation_retires_jobs_with_revoked_policy(tmp_path, environments, monkeypatch):
    import json
    from tools import process_registry as p
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy
    home = tmp_path / "profile"
    policy(home, backend="mxc", mxc_network=True)
    monkeypatch.setattr(p.process_registry, "_running", {})
    monkeypatch.setattr(p.process_registry, "_finished", {})
    killed = []
    def kill(sid, **kwargs):
        killed.append(sid)
        p.process_registry._running[sid].exited = True
        return {"status": "killed"}
    monkeypatch.setattr(p.process_registry, "kill_process", kill)
    with bound(home):
        reconcile_terminal_policy()
        job = p.process_registry._new_session("test", "task", "task", "session", str(tmp_path), terminal_backend="mxc")
        p.process_registry._running[job.id] = job
        policy(home, backend="mxc", mxc_network=False)
        json.loads(p._handle_process({"action": "poll", "session_id": job.id}))
        assert killed == [job.id]


def test_process_checkpoint_preserves_profile_and_backend(tmp_path, monkeypatch):
    import json
    from tools import process_registry as p
    from hermes_constants import hermes_home_key
    home = tmp_path / "profile"
    policy(home, backend="mxc")
    monkeypatch.setattr(p.process_registry, "_running", {})
    with bound(home):
        job = p.process_registry._new_session("test", "task", "task", "session", str(tmp_path), terminal_backend="mxc", pid=12345, host_start_time=1.0)
        p.process_registry._running[job.id] = job
        p.process_registry._write_checkpoint()
        payload = json.loads(p._checkpoint_path().read_text(encoding="utf-8"))
        entry = payload[0]
        assert entry["profile_home"] == hermes_home_key()
        assert entry["terminal_backend"] == "mxc"


def test_unowned_running_work_keeps_mxc_reconciliation_unavailable(tmp_path, environments, monkeypatch):
    from tools import process_registry as p
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy
    home = tmp_path / "profile"
    policy(home, backend="mxc")
    monkeypatch.setattr(p.process_registry, "_running", {"legacy": p.ProcessSession(id="legacy", command="test")})
    with bound(home):
        with pytest.raises(scope.TerminalPolicyUnavailable, match="unowned"):
            reconcile_terminal_policy()


def test_process_session_keeps_legacy_positional_task_id():
    from tools.process_registry import ProcessSession
    assert ProcessSession("id", "command", "task").task_id == "task"


def test_environment_cleanup_runs_in_its_own_profile(tmp_path, environments, monkeypatch):
    from hermes_constants import hermes_home_key
    from tools.terminal_tool_lifecycle import _cleanup_env
    a, b, work = tmp_path / "a", tmp_path / "b", tmp_path / "work"
    work.mkdir()
    policy(a, backend="local", cwd=str(work))
    policy(b, backend="local", cwd=str(work))
    with bound(a):
        env = acquire(environments, "owned-cleanup")
        owner = hermes_home_key()
    observed = []
    monkeypatch.setattr(env, "cleanup", lambda: observed.append(hermes_home_key()))
    with bound(b):
        _cleanup_env(env)
    assert observed == [owner]


def test_failed_kernel_retirement_remains_retryable(tmp_path, environments, monkeypatch):
    from tools import code_kernel
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy
    home = tmp_path / "profile"
    policy(home, backend="local")
    monkeypatch.setattr(code_kernel._REGISTRY, "kernels", {})
    attempts = []
    with bound(home):
        reconcile_terminal_policy()
        kernel = code_kernel.SessionKernel(("owner",))
        def fail():
            attempts.append(True)
            raise RuntimeError("cannot terminate")
        monkeypatch.setattr(kernel, "teardown", fail)
        code_kernel._REGISTRY.kernels[kernel.key] = kernel
        policy(home, backend="mxc")
        for _ in range(2):
            with pytest.raises(Exception, match="cannot terminate"):
                reconcile_terminal_policy()
            policy(home, backend="local")
    assert len(attempts) == 2


def test_terminal_config_reads_one_coherent_live_snapshot(tmp_path, monkeypatch):
    from tools import terminal_tool
    home, workspace = tmp_path / "profile", tmp_path / "workspace"
    workspace.mkdir()
    policy(home, backend="local", cwd=str(workspace))
    with bound(home):
        original = scope.build_profile_terminal_scope
        reads = []
        def build(*args, **kwargs):
            reads.append(True)
            return original(*args, **kwargs)
        monkeypatch.setattr(scope, "build_profile_terminal_scope", build)
        assert terminal_tool._get_env_config()["env_type"] == "local"
        assert len(reads) == 1


def test_standalone_without_cwd_keeps_launch_directory(tmp_path, monkeypatch):
    from tools import terminal_tool
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.setattr(terminal_tool, "_safe_getcwd", lambda: str(tmp_path))
    policy(tmp_path / "profile", backend="local")
    token = scope.set_terminal_scope(None)
    ht = set_hermes_home_override(None)
    try:
        assert terminal_tool._get_env_config()["cwd"] == str(tmp_path)
    finally:
        scope.reset_terminal_scope(token)
        reset_hermes_home_override(ht)


def test_home_only_writer_scope_overrides_an_outer_turn_binding(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    policy(a, backend="mxc")
    policy(b, backend="local")
    with bound(a):
        token = set_hermes_home_override(b)
        try:
            assert scope.get_live_terminal_config()["backend"] == "local"
        finally:
            reset_hermes_home_override(token)
        assert scope.get_live_terminal_config()["backend"] == "mxc"
