"""Backend review regressions over disposable profiles and recording leaves."""
from contextlib import contextmanager

import pytest
import yaml

from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from tools import terminal_scope as scope


def policy(home, **terminal):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"terminal": terminal}), encoding="utf-8")


@contextmanager
def bound(home):
    ht = set_hermes_home_override(home)
    tt = scope.install_profile_terminal_scope(home)
    try:
        yield
    finally:
        scope.reset_terminal_scope(tt)
        reset_hermes_home_override(ht)


@pytest.fixture
def local_kernels(monkeypatch):
    from tools import code_kernel as kernels
    registry = kernels.KernelRegistry(lambda kernel: kernel.teardown())
    monkeypatch.setattr(kernels, "_REGISTRY", registry)
    monkeypatch.setattr(kernels, "_KERNELS", registry.kernels)
    return kernels


def test_active_reset_keeps_kernel_available_to_policy_retirement(tmp_path, local_kernels):
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy
    home = tmp_path / "profile"
    policy(home, backend="local")
    with bound(home):
        reconcile_terminal_policy()
        kernel, _ = local_kernels._acquire_kernel(("session",), False)
        kernel.cell_authority = local_kernels.CellAuthority("task")
        with pytest.raises(RuntimeError, match="active"):
            local_kernels._acquire_kernel(kernel.key, True)
        assert local_kernels._REGISTRY.kernels[kernel.key] is kernel
        policy(home, backend="mxc")
        reconcile_terminal_policy()
        assert kernel.stop_event.is_set()
        assert not kernel.cell_authority.active


def test_concurrent_active_cell_reset_cannot_hide_from_transition(tmp_path, local_kernels, monkeypatch):
    import contextvars
    import io
    import threading
    from tools import code_execution_tool
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy
    home = tmp_path / "profile"
    policy(home, backend="local")
    entered, released = threading.Event(), threading.Event()
    results = []
    class Process:
        stdin = io.BytesIO()
        exited = False
        def poll(self):
            return 0 if self.exited else None
    def spawn(kernel, **kwargs):
        kernel.proc = Process()
    def kill(proc, **kwargs):
        proc.exited = True
        released.set()
    def await_cell(*args):
        entered.set()
        assert released.wait(10)
        return "interrupted", {}
    monkeypatch.setattr(local_kernels, "_spawn", spawn)
    monkeypatch.setattr(local_kernels, "_await_cell", await_cell)
    monkeypatch.setattr(code_execution_tool, "_kill_process_group", kill)
    kwargs = dict(task_id="task", mode="local", child_python="fixture", child_cwd=str(tmp_path),
        sandbox_tools=frozenset(), timeout=10, max_tool_calls=1, is_interrupted=lambda: False)
    with bound(home):
        context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(lambda: results.append(
            local_kernels.execute_in_session_kernel("pass", reset=False, **kwargs)),), daemon=True)
        thread.start()
        try:
            assert entered.wait(10)
            kernel = next(iter(local_kernels._REGISTRY.kernels.values()))
            with pytest.raises(RuntimeError, match="active"):
                local_kernels.execute_in_session_kernel("pass", reset=True, **kwargs)
            policy(home, backend="mxc")
            reconcile_terminal_policy()
            assert kernel.proc.poll() == 0 and not kernel.cell_authority.active
        finally:
            released.set()
            thread.join(10)
        assert not thread.is_alive() and results
        assert not local_kernels._REGISTRY.kernels


@pytest.mark.parametrize("action", ["discard", "shutdown", "idle", "cap"])
def test_failed_local_retirement_keeps_inventory(tmp_path, local_kernels, monkeypatch, action):
    home = tmp_path / "profile"
    policy(home, backend="local")
    with bound(home):
        kernel = local_kernels.SessionKernel(("old",))
        local_kernels._REGISTRY.kernels[kernel.key] = kernel
        def fail():
            raise RuntimeError("fixture retirement failed")
        monkeypatch.setattr(kernel, "teardown", fail)
        monkeypatch.setattr(local_kernels, "_lifecycle_limits", lambda: (1, 10))
        if action == "idle":
            kernel.last_used -= 100
        with pytest.raises(RuntimeError, match="fixture retirement failed"):
            if action == "discard":
                local_kernels._REGISTRY.discard(kernel.key, kernel)
            elif action == "shutdown":
                local_kernels._REGISTRY.shutdown()
            else:
                local_kernels._acquire_kernel(("new",), False)
        assert local_kernels._REGISTRY.kernels[kernel.key] is kernel


@pytest.mark.parametrize("returncode", [1, 0, 2, None])
def test_remote_retirement_uses_environment_returncode(returncode):
    from tools.code_kernel_remote import RemoteKernel
    class Transport:
        def execute(self, command, **kwargs):
            return {"output": "", "returncode": returncode if command.startswith("kill -0") else 0}
    kernel = RemoteKernel(Transport(), "ssh", "/tmp/fixture-kernel", "42", "token", "owner")
    if returncode == 1:
        kernel.kill(strict=True)
    else:
        with pytest.raises(RuntimeError, match="could not be confirmed"):
            kernel.kill(strict=True)


@pytest.mark.parametrize("sweep", ["idle", "cap"])
def test_remote_eviction_respects_bound_profile_owner(tmp_path, monkeypatch, sweep):
    from tools import code_kernel_remote as remote, code_kernel
    from tools.terminal_policy_lifecycle import bind_environment, terminal_policy_guard
    registry = code_kernel.KernelRegistry(lambda kernel: kernel.kill(strict=True))
    monkeypatch.setattr(remote, "_REGISTRY", registry)
    monkeypatch.setattr(remote, "_REMOTE_KERNELS", registry.kernels)
    monkeypatch.setattr(code_kernel, "_lifecycle_limits", lambda: (1, 1800))
    monkeypatch.setattr(remote, "_run_attached_cell", lambda *a, **kw: {"status": "success"})
    class Transport:
        def __init__(self):
            self.calls = []
        def execute(self, command, **kwargs):
            self.calls.append(command)
            return {"output": "", "returncode": 1 if command.startswith("kill -0") else 0}
    def spawn(env, backend, owner, *a, **kw):
        return remote.RemoteKernel(env, backend, "/tmp/fixture", "42", "token", owner)
    monkeypatch.setattr(remote, "_spawn_remote_kernel", spawn)
    a, b = tmp_path / "a", tmp_path / "b"
    for home in (a, b):
        policy(home, backend="ssh")
    def acquire(home, task):
        with bound(home), terminal_policy_guard() as (owner, fingerprint):
            env = Transport()
            bind_environment(env, owner, fingerprint)
            remote.execute_in_remote_kernel("pass", env=env, env_type="ssh", task_env_id=task,
                sandbox_tools=frozenset(), timeout=1, max_tool_calls=1, reset=False)
            return env
    a_env = acquire(a, "a-task")
    a_key, a_kernel = next(iter(registry.kernels.items()))
    if sweep == "idle":
        a_kernel.last_used -= 2000
    b_env = acquire(b, "b-task")
    assert registry.kernels[a_key] is a_kernel
    assert a_env.calls == []
    b_key = next(key for key in registry.kernels if key != a_key)
    acquire(a, "a-new-task")
    assert a_key not in registry.kernels
    assert b_key in registry.kernels and b_env.calls == []
    assert any(command.startswith("kill -0") for command in a_env.calls)


def test_remote_failed_retirement_retains_registry_entry(tmp_path, monkeypatch):
    from tools import code_kernel_remote as remote
    home = tmp_path / "profile"
    policy(home, backend="ssh")
    monkeypatch.setattr(remote._REGISTRY, "kernels", {})
    monkeypatch.setattr(remote, "_REMOTE_KERNELS", remote._REGISTRY.kernels)
    class Transport:
        def execute(self, *args, **kwargs):
            raise RuntimeError("fixture transport unavailable")
    with bound(home):
        kernel = remote.RemoteKernel(Transport(), "ssh", "/tmp/fixture", "42", "token", "owner")
        key = remote._kernel_key("owner", "ssh", "task", frozenset())
        remote._REGISTRY.kernels[key] = kernel
        with pytest.raises(RuntimeError, match="fixture transport unavailable"):
            remote._REGISTRY.discard(key, kernel)
        assert remote._REGISTRY.kernels[key] is kernel


@pytest.mark.parametrize("field,value", [
    ("mxc_network", None), ("mxc_debug", None),
    ("mxc_readwrite_paths", None), ("mxc_readonly_paths", None),
    ("mxc_network", "not-a-boolean"), ("mxc_readwrite_paths", "not-a-list"),
    ("mxc_network", 1), ("mxc_network", "true"),
    ("mxc_readwrite_paths", "[]"), ("mxc_readonly_paths", "not-a-list"),
])
def test_mxc_fields_are_strict_only_when_active(tmp_path, monkeypatch, field, value):
    from tools.terminal_tool import _get_env_config
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_MXC_READWRITE_PATHS", '["C:/stale-grant"]')
    monkeypatch.setenv("TERMINAL_MXC_NETWORK", "true")
    ht = set_hermes_home_override(None)
    tt = scope.set_terminal_scope(None)
    try:
        policy(home, backend="mxc", **{field: value})
        with pytest.raises(scope.TerminalPolicyUnavailable):
            scope.get_live_terminal_config()
        policy(home, backend="local", **{field: value})
        assert scope.get_live_terminal_config()["backend"] == "local"
        assert _get_env_config()["env_type"] == "local"
    finally:
        scope.reset_terminal_scope(tt)
        reset_hermes_home_override(ht)


@pytest.mark.parametrize("backend", ["MXC", " mXc "])
def test_backend_identity_is_canonical_across_admission_and_execution(tmp_path, backend):
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy, policy_fingerprint
    from tools.terminal_tool import _get_env_config
    home, work = tmp_path / "profile", tmp_path / "work"
    work.mkdir()
    policy(home, backend=backend, cwd=str(work))
    with bound(home):
        assert scope.get_live_terminal_config()["backend"] == "mxc"
        assert _get_env_config()["env_type"] == "mxc"
        reconcile_terminal_policy()
        fingerprint = policy_fingerprint()
        policy(home, backend="mxc", cwd=str(work))
        assert fingerprint == policy_fingerprint()


@pytest.mark.parametrize("binding", ["standalone", "profile"])
def test_terminal_reader_matches_effective_expansion_and_managed_overlay(tmp_path, monkeypatch, binding):
    from hermes_cli.config import load_config
    from tools.terminal_tool import _get_env_config
    home, managed = tmp_path / "profile", tmp_path / "managed"
    policy(home, backend="ssh", ssh_host="${REVIEW_FIXTURE_HOST}", ssh_user="user-value")
    policy(managed, ssh_user="managed-value")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("REVIEW_FIXTURE_HOST", "expanded-host")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "expanded-host")
    ht = set_hermes_home_override(home if binding == "profile" else None)
    tt = scope.install_profile_terminal_scope(home) if binding == "profile" else scope.set_terminal_scope(None)
    try:
        expected = load_config()["terminal"]
        live = scope.get_live_terminal_config()
        assert live["ssh_host"] == expected["ssh_host"] == "expanded-host"
        assert live["ssh_user"] == expected["ssh_user"] == "managed-value"
        assert _get_env_config()["ssh_host"] == "expanded-host"
        (managed / "config.yaml").write_text("terminal: [", encoding="utf-8")
        with pytest.raises(scope.TerminalPolicyUnavailable):
            scope.get_live_terminal_config()
    finally:
        scope.reset_terminal_scope(tt)
        reset_hermes_home_override(ht)


@pytest.mark.parametrize("change", ["ssh_host", "configured_cwd", "selected_cwd"])
def test_stale_plan_cannot_be_published_under_new_authority(tmp_path, monkeypatch, change):
    from tools import terminal_tool as terminal
    home, old, new = tmp_path / "profile", tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    backend = "ssh" if change == "ssh_host" else "mxc"
    policy(home, backend=backend, cwd=str(old), ssh_host="old-host")
    monkeypatch.setattr(terminal, "_active_environments", {})
    monkeypatch.setattr(terminal, "_last_activity", {})
    monkeypatch.setattr(terminal, "_task_env_overrides", {})
    monkeypatch.setattr(terminal, "_start_cleanup_thread", lambda: None)
    created = []
    class Environment:
        def __init__(self, config, backend, **kw):
            self.env_type, self.cwd = backend, kw["cwd"]
            created.append(config)
        def execute(self, *args, **kwargs):
            return {"output": "", "returncode": 0}
        def cleanup(self):
            pass
    monkeypatch.setattr(terminal, "_create_configured_env", Environment)
    with bound(home):
        plan = terminal._plan_execution("true", task_id="task", timeout=None, background=False, _host_local=False)
        if change == "selected_cwd":
            terminal.register_task_env_overrides("task", {"cwd": str(new)})
        else:
            policy(home, backend=backend, cwd=str(new if change == "configured_cwd" else old), ssh_host="new-host")
        with pytest.raises(scope.TerminalPolicyUnavailable, match="planning"):
            terminal._acquire_env(plan, "task")
        assert not created and not terminal._active_environments
        fresh = terminal._plan_execution("true", task_id="task", timeout=None, background=False, _host_local=False)
        env = terminal._acquire_env(fresh, "task")
        assert env.env_type == backend and len(created) == 1
        if change == "ssh_host":
            assert created[0]["ssh_host"] == "new-host"
        else:
            assert env.cwd == str(new)


@pytest.mark.parametrize("backend", ["mxc", " MXC "])
def test_process_actions_share_conversation_ownership_and_handoff(tmp_path, monkeypatch, backend):
    import json
    from hermes_constants import hermes_home_key
    from tools import process_registry as processes
    from tools.approval_context import set_current_session_key, reset_current_session_key
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy
    from agent.delegation_context import delegated_child_context
    home = tmp_path / "profile"
    policy(home, backend=backend)
    registry = processes.process_registry
    monkeypatch.setattr(registry, "_running", {})
    monkeypatch.setattr(registry, "_finished", {})
    leaves, refreshed = [], []
    for action in processes._SESSION_ACTIONS:
        monkeypatch.setitem(processes._SESSION_ACTIONS, action,
            (lambda sid, args: leaves.append(sid) or {"status": "fixture"}, False))
    monkeypatch.setattr(registry, "_refresh_detached_session", lambda s: refreshed.append(s.id) or s)
    token = set_current_session_key("caller-session")
    try:
        with bound(home):
            reconcile_terminal_policy()
            for sid, owner, session in [("proc_peer", "peer-task", "peer-session"),
                    ("proc_own", "earlier-turn", "caller-session"),
                    ("proc_child", "sa-child", "caller-session")]:
                registry._running[sid] = processes.ProcessSession(id=sid, command="fixture",
                    task_id="default", owner_task_id=owner, session_key=session,
                    profile_home=hermes_home_key(), terminal_backend="mxc")
            for action in processes._SESSION_ACTIONS:
                for sid in ("proc_peer", "proc_child"):
                    result = json.loads(processes._handle_process({"action": action, "session_id": sid}, task_id="caller-task"))
                    assert "error" in result, (action, sid, result)
                assert json.loads(processes._handle_process({"action": action, "session_id": "proc_own"}, task_id="caller-task"))["status"] == "fixture"
            listing = json.loads(processes._handle_process({"action": "list"}, task_id="caller-task"))
            assert [row["session_id"] for row in listing["processes"]] == ["proc_own"]
            assert refreshed == ["proc_own"]
            with delegated_child_context("child-session"):
                assert "error" in json.loads(processes._handle_process({"action": "poll", "session_id": "proc_own"}, task_id="sa-child"))
                assert json.loads(processes._handle_process({"action": "poll", "session_id": "proc_child"}, task_id="sa-child"))["status"] == "fixture"
                assert registry.transfer_ownership("proc_child", from_owner="sa-child", to_owner="caller-task",
                    to_task_id="default", to_session_key="caller-session") is not None
                assert "error" in json.loads(processes._handle_process({"action": "poll", "session_id": "proc_child"}, task_id="sa-child"))
            assert json.loads(processes._handle_process({"action": "poll", "session_id": "proc_child"}, task_id="caller-task"))["status"] == "fixture"
    finally:
        reset_current_session_key(token)


def test_failed_reset_can_retry_retirement(tmp_path, local_kernels, monkeypatch):
    home = tmp_path / "retry-profile"
    policy(home, backend="local")
    with bound(home):
        kernel, _ = local_kernels._acquire_kernel(("retry",), False)
        kernel.attached = 0
        attempts = []
        def teardown():
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("transient teardown")
        monkeypatch.setattr(kernel, "teardown", teardown)
        with pytest.raises(RuntimeError, match="transient"):
            local_kernels._acquire_kernel(kernel.key, True)
        with pytest.raises(RuntimeError, match="retirement"):
            local_kernels._acquire_kernel(kernel.key, False)
        replacement, _ = local_kernels._acquire_kernel(kernel.key, True)
        assert replacement is not kernel and len(attempts) == 2


def test_shared_cache_mismatch_retires_only_current_owner(tmp_path, monkeypatch):
    from tools import terminal_tool as terminal
    monkeypatch.setattr(terminal, "_active_environments", {})
    monkeypatch.setattr(terminal, "_last_activity", {})
    monkeypatch.setattr(terminal, "_task_env_overrides", {})
    monkeypatch.setattr(terminal, "_start_cleanup_thread", lambda: None)
    class Environment:
        def __init__(self, config, backend, **kwargs):
            self.env_type, self.cwd, self.cleaned = backend, kwargs["cwd"], False
        def execute(self, command, **kwargs):
            return {"output": command, "returncode": 0}
        def cleanup(self):
            self.cleaned = True
    monkeypatch.setattr(terminal, "_create_configured_env", Environment)
    a, b = tmp_path / "a", tmp_path / "b"
    for home in (a, b):
        policy(home, backend="docker", docker_shared_container_key="fixture-shared")
        with bound(home):
            plan = terminal._plan_execution("", task_id="task", timeout=None, background=False, _host_local=False)
            env = terminal._acquire_env(plan, "task")
    original = terminal._acquire_env_unfenced
    def changed(*args):
        result = original(*args)
        policy(a, backend="mxc")
        return result
    monkeypatch.setattr(terminal, "_acquire_env_unfenced", changed)
    with bound(a):
        plan = terminal._plan_execution("", task_id="task", timeout=None, background=False, _host_local=False)
        with pytest.raises(scope.TerminalPolicyUnavailable):
            terminal._acquire_env(plan, "task")
    with bound(b):
        assert not env.cleaned
        assert env in terminal._active_environments.values()
        assert env.execute("still authorized")["returncode"] == 0


def test_explicit_shared_docker_remains_usable_across_profiles(tmp_path, monkeypatch):
    from tools import terminal_tool as terminal
    from tools.terminal_policy_lifecycle import reconcile_terminal_policy
    monkeypatch.setattr(terminal, "_active_environments", {})
    monkeypatch.setattr(terminal, "_last_activity", {})
    monkeypatch.setattr(terminal, "_task_env_overrides", {})
    monkeypatch.setattr(terminal, "_start_cleanup_thread", lambda: None)
    class Environment:
        def __init__(self, config, backend, **kw):
            self.env_type, self.cwd, self.cleaned = backend, kw["cwd"], False
        def execute(self, command, **kwargs):
            return {"output": command, "returncode": 0}
        def cleanup(self):
            self.cleaned = True
    monkeypatch.setattr(terminal, "_create_configured_env", Environment)
    a, b = tmp_path / "a", tmp_path / "b"
    for home in (a, b):
        policy(home, backend="docker", docker_shared_container_key="fixture-shared")
    envs = []
    for home in (a, b, a):
        with bound(home):
            plan = terminal._plan_execution("fixture", task_id="task", timeout=None, background=False, _host_local=False)
            env = terminal._acquire_env(plan, "task")
            envs.append(env)
            assert env.execute("fixture")["returncode"] == 0
    assert envs[0] is envs[1] is envs[2]
    with bound(a):
        policy(a, backend="mxc")
        reconcile_terminal_policy()
        with pytest.raises(scope.TerminalPolicyUnavailable):
            env.execute("must refuse")
    with bound(b):
        assert not env.cleaned
        assert env.execute("still owned by b")["returncode"] == 0
