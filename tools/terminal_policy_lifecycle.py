"""Profile-local terminal policy reconciliation over the existing resource registries.

The profile lock is a publication/drain barrier, not another job registry. Config
is read at each admission, so edits made by another process fence cache reuse too.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import threading
import sys

from hermes_constants import hermes_home_key
from tools.terminal_scope import get_live_terminal_config, TerminalPolicyUnavailable

_locks_lock = threading.Lock()
_locks: dict[str, threading.Condition] = {}
_active_calls: dict[str, int] = {}
_applied: dict[str, str] = {}
_failed: set[str] = set()
_retiring = ContextVar("terminal_policy_retiring", default=None)
_admission = threading.local()


def policy_fingerprint(*, include_cwd: bool = False) -> str:
    config = get_live_terminal_config()
    # cwd placeholders are resolved by each surface and are not a profile-wide
    # policy generation. Per-session workspace roots remain on the environment.
    if not include_cwd:
        config.pop("cwd", None)
    return json.dumps(config, sort_keys=True, separators=(",", ":"))


def _profile_lock(owner: str):
    with _locks_lock:
        return _locks.setdefault(owner, threading.Condition(threading.RLock()))


def _retire_environments(owner: str, only=None) -> None:
    from tools import terminal_tool as terminal
    from tools.terminal_tool_lifecycle import _cleanup_env, _clear_file_ops_cache
    with terminal._env_lock:
        owned = [(key, env) for key, env in terminal._active_environments.items()
                 if owner in getattr(env, "_terminal_policy_authorities", {getattr(env, "_terminal_policy_owner", None): None})
                 and (only is None or env is only)]
    for key, env in owned:
        authorities = getattr(env, "_terminal_policy_authorities", {})
        if len(authorities) > 1:
            # Explicit shared Docker authority is opt-in per profile. Revoking
            # one participant must neither keep its access nor kill its peers.
            authorities.pop(owner, None)
            _clear_file_ops_cache(key)
            continue
        env._terminal_policy_retired = True
        _clear_file_ops_cache(key)
        _cleanup_env(env, force_remove=True)
        wait = getattr(env, "wait_for_cleanup", None)
        if wait is not None:
            wait(timeout=15.0)
        with terminal._env_lock:
            if terminal._active_environments.get(key) is env:
                terminal._active_environments.pop(key)
                terminal._last_activity.pop(key, None)


def reconcile_terminal_policy() -> None:
    """Apply the ACTIVE profile's live policy, raising if retirement fails.

    Call after persistence while still in the writer's profile scope. Never
    sweep an unowned resource or another profile's resources.
    """
    owner = hermes_home_key()
    with _profile_lock(owner):
        fingerprint = policy_fingerprint()
        mxc = get_live_terminal_config()["backend"] == "mxc"
        if mxc:
            _check_unowned_work()
        if _applied.get(owner) == fingerprint and owner not in _failed:
            return
        while _active_calls.get(owner, 0):
            _profile_lock(owner).wait()
        token = _retiring.set(owner)
        try:
            if owner in _applied or mxc:
                _retire_workers(owner)
                _retire_environments(owner)
        except Exception:
            _failed.add(owner)
            raise
        finally:
            _retiring.reset(token)
        _failed.discard(owner)
        _applied[owner] = fingerprint


def _check_unowned_work() -> None:
    """Legacy live work cannot be attributed by guessing the current profile."""
    terminal = sys.modules.get("tools.terminal_tool")
    if terminal is not None:
        with terminal._env_lock:
            if any(not getattr(env, "_terminal_policy_owner", None) for env in terminal._active_environments.values()):
                raise TerminalPolicyUnavailable("Cannot enable sandbox with unowned terminal environments; stop that work first")
    for name in ("tools.code_kernel", "tools.code_kernel_remote"):
        registry = getattr(sys.modules.get(name), "_REGISTRY", None)
        if registry is not None:
            with registry.lock:
                if any(not getattr(kernel, "profile_home", None) for kernel in registry.kernels.values()):
                    raise TerminalPolicyUnavailable("Cannot enable sandbox with unowned kernels; stop that work first")
    registry = getattr(sys.modules.get("tools.process_registry"), "process_registry", None)
    if registry is not None:
        with registry._lock:
            if any(not job.profile_home and not job.exited for job in registry._running.values()):
                raise TerminalPolicyUnavailable("Cannot enable sandbox with unowned background work; stop that work first")


def retire_workspace_environment(env) -> None:
    """Retire only the environment whose human-selected workspace changed."""
    owner = hermes_home_key()
    with _profile_lock(owner):
        while _active_calls.get(owner, 0):
            _profile_lock(owner).wait()
        token = _retiring.set(owner)
        try:
            _retire_environments(owner, only=env)
        finally:
            _retiring.reset(token)


def _retire_workers(owner: str) -> None:
    # Import only registries already in use; reconciliation must not initialize
    # optional execution services or adopt unowned legacy work.
    for name in ("tools.code_kernel", "tools.code_kernel_remote"):
        module = sys.modules.get(name)
        registry = getattr(module, "_REGISTRY", None)
        if registry is None:
            continue
        with registry.lock:
            owned = [(key, kernel) for key, kernel in registry.kernels.items()
                     if getattr(kernel, "profile_home", None) == owner]
        for key, kernel in owned:
            registry.discard(key, kernel)
    module = sys.modules.get("tools.process_registry")
    registry = getattr(module, "process_registry", None)
    if registry is not None:
        with registry._lock:
            owned = [job for job in registry._running.values()
                     if job.profile_home == owner and not job.exited]
        for job in owned:
            result = registry.kill_process(job.id, source="terminal.policy", consume_output=True)
            if result.get("status") not in {"killed", "already_exited"}:
                raise TerminalPolicyUnavailable(f"Cannot retire background process {job.id}: {result.get('error', result)}")


@contextmanager
def terminal_policy_guard():
    """Serialize admission/publication with reconciliation for this profile."""
    owner = hermes_home_key()
    with _profile_lock(owner):
        previous = getattr(_admission, "current", None)
        if previous is not None and previous[0] == owner:
            yield previous
            return
        reconcile_terminal_policy()
        _admission.current = (owner, _applied[owner])
        try:
            yield _admission.current
        finally:
            _admission.current = previous


def bind_environment(env, owner: str, fingerprint: str) -> None:
    if getattr(env, "_terminal_policy_wrapped", False):
        env._terminal_policy_authorities[owner] = fingerprint
        return
    env._terminal_policy_owner = owner
    env._terminal_policy_fingerprint = fingerprint
    env._terminal_policy_authorities = {owner: fingerprint}
    env._terminal_policy_retired = False
    from contextvars import copy_context
    env._terminal_cleanup_context = copy_context()
    from functools import wraps

    def guarded(method):
        @wraps(method)
        def call(*args, **kwargs):
            if _retiring.get() in env._terminal_policy_authorities:
                return method(*args, **kwargs)
            with terminal_policy_guard() as (active_owner, active_fingerprint):
                check_environment(env, active_owner, active_fingerprint)
                _active_calls[active_owner] = _active_calls.get(active_owner, 0) + 1
            try:
                return method(*args, **kwargs)
            finally:
                with _profile_lock(active_owner):
                    _active_calls[active_owner] -= 1
                    _profile_lock(active_owner).notify_all()
        return call

    # Retained references (including ShellFileOperations) cannot spawn after
    # retirement. Hold the barrier through foreground execution: a writer drains
    # that operation before it can truthfully confirm the new policy.
    for name in ("execute", "start_background"):
        method = getattr(env, name, None)
        if callable(method):
            setattr(env, name, guarded(method))
    env._terminal_policy_wrapped = True


def check_environment(env, owner: str, fingerprint: str) -> None:
    authorities = getattr(env, "_terminal_policy_authorities", None)
    if authorities is not None:
        authorized = authorities.get(owner) == fingerprint
    else:
        authorized = (getattr(env, "_terminal_policy_owner", owner) == owner
                      and getattr(env, "_terminal_policy_fingerprint", fingerprint) == fingerprint)
    if (getattr(env, "_terminal_policy_retired", False)
            or not authorized):
        raise TerminalPolicyUnavailable("Terminal policy changed; retry with the current environment")
