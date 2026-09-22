"""Per-turn terminal scope: profile-scoped TERMINAL_* policy.

Multiplexed surfaces (gateway, dashboard/TUI, cron) serve several profiles from one process;
mirroring terminal settings into ``os.environ`` let the first profile pin its backend onto
everyone else (sandbox escape). Like ``agent/secret_scope.py``, a ContextVar holds the active
profile's COMPLETE ``TERMINAL_*`` policy; while bound, ``terminal_env`` resolves ONLY from it
(omitted keys -> defined default, never ambient env). If the policy cannot be resolved a
*refusal* scope is installed and terminal execution raises :class:`TerminalPolicyUnavailable`.
"""

from __future__ import annotations

import logging
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

# None = no scope bound (process-env behavior); dict = complete policy; Refusal = resolution failed.
_terminal_scope_var: ContextVar = ContextVar("hermes_terminal_scope", default=None)
_read_snapshot: ContextVar = ContextVar("hermes_terminal_read_snapshot", default=None)

# Keys whose default lives in terminal_tool.py, not DEFAULT_CONFIG (which wins on overlap);
# without them the projection is not total.
_TOOL_LEVEL_DEFAULTS: Dict[str, Any] = {
    "cwd": ".", "ssh_host": "", "ssh_user": "", "ssh_port": 22, "ssh_key": "",
    "docker_orphan_reaper": True, "docker_persist_across_processes": True,
    "sandbox_dir": "", "lifetime_seconds": 300, "docker_shared_container_key": "",
    "home_mode": "auto",
}


class TerminalPolicyUnavailable(Exception):
    """The routed profile's ``.env``/``config.yaml`` exists but cannot be read/parsed."""


class ProfileTerminalScope(dict):
    """A profile binding, not a turn-lifetime snapshot of its execution authority."""

    def __init__(self, home, env_overlay=None):
        super().__init__()
        self.home = Path(home)
        self.env_overlay = dict(env_overlay or {})


class TerminalPolicyRefusal(Dict[str, str]):
    """Marker scope (empty dict subclass) installed when policy resolution failed."""

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason


def set_terminal_scope(mapping: Optional[Dict[str, str]]) -> Token:
    """Install *mapping* as the current context's terminal policy."""
    return _terminal_scope_var.set(mapping)


def reset_terminal_scope(token: Token) -> None:
    _terminal_scope_var.reset(token)


def get_terminal_scope() -> Optional[Dict[str, str]]:
    """The active scope mapping/refusal, or ``None`` when no scope is bound."""
    return _terminal_scope_var.get()


def enforce_no_refusal() -> None:
    """Raise when the active scope is a refusal scope (fail closed).

    Execution paths (terminal tool, execute_code) call this before spawning anything: under a refusal scope
    the profile's terminal policy could not be resolved, and running with the launch process's ambient
    policy is exactly the authority leak this module closes (#68559 requires refusal, not fallback).
    Non-scoped and policy-scoped contexts pass silently.
    """
    scope = _terminal_scope_var.get()
    if isinstance(scope, TerminalPolicyRefusal):
        raise TerminalPolicyUnavailable(
            f"terminal policy unavailable for this profile: {scope.reason}")


def terminal_env(name: str, default: str = "") -> str:
    """Authoritative read of a ``TERMINAL_*`` variable.

    No scope: process env, then *default*. Refusal scope: raise. Policy scope: ONLY the
    policy; a missing key yields *default*, never os.environ.
    """
    scope = _read_snapshot.get()
    if scope is None:
        scope = _live_terminal_scope()
    value = scope.get(name)
    return default if value is None else str(value)


def _live_terminal_scope() -> dict:
    enforce_no_refusal()
    scope = _terminal_scope_var.get()
    if isinstance(scope, ProfileTerminalScope):
        from hermes_constants import get_hermes_home_override
        override = get_hermes_home_override()
        if override is not None and Path(override).resolve() != scope.home.resolve():
            return build_profile_terminal_scope(override)
        return build_profile_terminal_scope(scope.home, env_overlay=scope.env_overlay)
    if scope is not None:
        values = dict(scope)
        _validate_policy_values(values)
        return values
    from hermes_constants import get_hermes_home, get_hermes_home_override
    # A home-only control-plane scope must never borrow the launch environment.
    overlay = None if get_hermes_home_override() is not None else os.environ
    return build_profile_terminal_scope(get_hermes_home(), env_overlay=overlay, resolve_cwd=False)


@contextmanager
def terminal_config_snapshot():
    """One consistent read for the terminal config parser; never turn-lifetime."""
    if _read_snapshot.get() is not None:
        yield
        return
    token = _read_snapshot.set(_live_terminal_scope())
    try:
        yield
    finally:
        _read_snapshot.reset(token)


def get_live_terminal_config() -> dict:
    """Strict effective terminal settings for the active profile (config-name keys).

    Shares the execution reader; explicit false/empty values remain authoritative.
    Legacy env-only launches are overlays only for their own launch profile.
    """
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP
    values = _read_snapshot.get()
    if values is None:
        values = _live_terminal_scope()
    result = {key: values[var] for key, var in TERMINAL_CONFIG_ENV_MAP.items() if var in values}
    for key in ("mxc_readwrite_paths", "mxc_readonly_paths"):
        if key in result:
            try:
                result[key] = json.loads(result[key])
            except (ValueError, TypeError):
                # Inactive backend settings are inert, not a reason to disable
                # unrelated tools. Active policy was validated by the reader.
                pass
    for key in ("mxc_network", "mxc_debug"):
        if key in result and result[key].lower() in {"true", "false", "0", "1", "yes", "no"}:
            result[key] = result[key].lower() in {"true", "1", "yes"}
    return result


def build_profile_terminal_scope(
    hermes_home: "Any", *, env_overlay: Optional[Dict[str, str]] = None,
    resolve_cwd: bool = True) -> Dict[str, str]:
    """Build the COMPLETE effective ``TERMINAL_*`` policy for a profile home.

    Projection: ``DEFAULT_CONFIG['terminal']`` <- profile ``.env`` TERMINAL_* <- *env_overlay*
    <- profile ``config.yaml`` ``terminal:``. Total by construction, so a bound scope never
    widens back to ambient authority. Raises :class:`TerminalPolicyUnavailable` if a present
    file is unreadable.

    *env_overlay* is a TRUSTED ``TERMINAL_*`` mapping captured from the launch process before
    multiplexing began (``tui_gateway/launch_profile_policy.py``): the launch profile's
    env-only policy (``TERMINAL_ENV=ssh`` from systemd, ``op run``, a launcher bridge) has no
    file to rebuild it from, and reading live ``os.environ`` here is the leak this module
    closes. It sits where the process env sits in the standalone bridge — explicit YAML keys
    still win (``apply_terminal_config_to_env``).
    """
    from hermes_cli.config import TERMINAL_CONFIG_ENV_MAP, _terminal_env_value, _expand_env_vars, _deep_merge
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from hermes_cli.managed_scope import get_managed_dir

    home = Path(hermes_home)
    scope: Dict[str, str] = ProfileTerminalScope(home, env_overlay)

    def _apply(mapping: Dict[str, Any]) -> None:
        for cfg_key, value in mapping.items():
            # cwd placeholders are resolved per-surface later; not a policy value.
            if value is None or (cfg_key == "cwd" and str(value).strip() in {".", "auto", "cwd"}):
                continue
            env_var = TERMINAL_CONFIG_ENV_MAP.get(cfg_key)
            if env_var:
                # List/dict config values must be JSON (same contract as
                # apply_terminal_config_to_env). str() yields Python repr, which
                # json.loads in terminal_tool rejects.
                scope[env_var] = _terminal_env_value(value)

    _apply({**_TOOL_LEVEL_DEFAULTS, **(DEFAULT_CONFIG.get("terminal") or {})})
    env_path = home / ".env"
    if env_path.exists():
        # load_env_file swallows OSError by design (secret scope fails soft); an unreadable
        # profile .env must fail closed here.
        try:
            env_path.read_bytes()
        except Exception as exc:
            raise TerminalPolicyUnavailable(f"cannot read {env_path}: {exc}") from exc
        from agent.secret_scope import load_env_file

        scope.update((k, str(v)) for k, v in load_env_file(env_path).items()
                     if k.startswith("TERMINAL_"))
    if env_overlay:
        scope.update((k, str(v)) for k, v in env_overlay.items() if k.startswith("TERMINAL_"))
    # Use the effective loader's expansion and managed-leaf precedence, but
    # never its last-known-good/default fallback for an unreadable authority.
    raw_terminal = _read_terminal_authority(home / "config.yaml")
    terminal = _expand_env_vars(raw_terminal)
    managed_dir = get_managed_dir()
    if managed_dir is not None:
        terminal = _deep_merge(terminal, _expand_env_vars(
            _read_terminal_authority(managed_dir / "config.yaml")))
    if str(terminal.get("backend", scope.get("TERMINAL_ENV", "local"))).strip().lower() == "mxc":
        for key, expected in (("mxc_network", bool), ("mxc_debug", bool),
                              ("mxc_readwrite_paths", list), ("mxc_readonly_paths", list)):
            if key in terminal and not isinstance(terminal[key], expected):
                raise TerminalPolicyUnavailable(f"terminal.{key} must be a {expected.__name__}")
    _apply(terminal)
    _validate_policy_values(scope)
    if resolve_cwd:
        _resolve_scope_cwd_placeholder(scope)
    return scope


def _read_terminal_authority(config_path: Path) -> dict:
    """Read explicit keys only; missing files differ from invalid authority."""
    try:
        from hermes_cli.config import fast_safe_load
        with open(config_path, encoding="utf-8-sig") as f:
            raw = fast_safe_load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise TerminalPolicyUnavailable(f"cannot parse {config_path}: {exc}") from exc
    if raw is not None and not isinstance(raw, dict):
        raise TerminalPolicyUnavailable(f"{config_path} must contain a mapping")
    terminal = (raw or {}).get("terminal", {})
    if not isinstance(terminal, dict):
        raise TerminalPolicyUnavailable("terminal must be a mapping")
    if "backend" in terminal:
        backend = terminal["backend"]
        if not isinstance(backend, str) or not backend.strip():
            raise TerminalPolicyUnavailable("terminal.backend must be a nonempty name")
    return terminal


def _validate_policy_values(scope: dict) -> None:
    backend = scope.get("TERMINAL_ENV", "local")
    if not isinstance(backend, str) or not backend.strip():
        raise TerminalPolicyUnavailable("terminal.backend must be a nonempty name")
    scope["TERMINAL_ENV"] = backend.strip().lower()
    if scope["TERMINAL_ENV"] != "mxc":
        return
    for name in ("TERMINAL_MXC_READWRITE_PATHS", "TERMINAL_MXC_READONLY_PATHS"):
        try:
            paths = json.loads(scope.get(name, "[]"))
        except (ValueError, TypeError) as exc:
            raise TerminalPolicyUnavailable(f"{name} must be a JSON path list") from exc
        if not isinstance(paths, list) or any(not isinstance(p, str) or not p.strip() for p in paths):
            raise TerminalPolicyUnavailable(f"{name} must be a path list")
    for name in ("TERMINAL_MXC_NETWORK", "TERMINAL_MXC_DEBUG"):
        if scope.get(name, "false").lower() not in {"true", "false", "0", "1", "yes", "no"}:
            raise TerminalPolicyUnavailable(f"{name} must be a boolean")


def _resolve_scope_cwd_placeholder(scope: Dict[str, str]) -> None:
    """Give a scope with no explicit ``terminal.cwd`` the same resolved ``TERMINAL_CWD`` a standalone
    gateway computes at import (``gateway/run.py``: local backend → ``$HOME``; docker with the
    workspace mount → the host cwd signal; other backends → unset). Without it a routed turn's
    ``resolve_agent_cwd()`` falls back to the multiplexer PROCESS cwd (wherever ``hermes gateway``
    was launched), so the system prompt, context-file discovery and the local terminal all run in
    a directory the profile's standalone gateway would never have used."""
    if scope.get("TERMINAL_CWD"):
        return
    from gateway.cwd_placeholder import resolve_placeholder_terminal_cwd

    resolved = resolve_placeholder_terminal_cwd(
        configured_cwd="", terminal_backend=scope.get("TERMINAL_ENV", ""),
        messaging_cwd=None,
        docker_mount_cwd_to_workspace=scope.get(
            "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "false").strip().lower() in {"true", "1", "yes"},
        home_fallback=str(Path.home()),
    )
    if resolved:
        scope["TERMINAL_CWD"] = resolved


def install_profile_terminal_scope(
    hermes_home: "Any", *, env_overlay: Optional[Dict[str, str]] = None) -> Token:
    """Build AND install a profile's policy; on failure install the refusal scope. Never raises."""
    try:
        return set_terminal_scope(build_profile_terminal_scope(hermes_home, env_overlay=env_overlay))
    except TerminalPolicyUnavailable as exc:
        logger.warning("terminal policy unavailable: %s", exc)
        return _terminal_scope_var.set(TerminalPolicyRefusal(str(exc)))


@contextmanager
def install_and_reset_profile_terminal_scope(hermes_home: "Any") -> Iterator[None]:
    """Install the profile's terminal policy for a bounded turn/fire. Never raises."""
    token = install_profile_terminal_scope(hermes_home)
    try:
        yield
    finally:
        reset_terminal_scope(token)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.

def install_refusal_scope(reason: str) -> Token:
    """Install a refusal scope after :class:`TerminalPolicyUnavailable`.

    Terminal execution under this scope is rejected (fail closed) instead of
    running under the launch process's ambient policy.
    """
    return _terminal_scope_var.set(TerminalPolicyRefusal(reason))

@contextmanager
def terminal_scope(mapping: Optional[Dict[str, str]]) -> Iterator[None]:
    """Context manager form of set/reset_terminal_scope."""
    token = set_terminal_scope(mapping)
    try:
        yield
    finally:
        reset_terminal_scope(token)
# ---- END PLUGIN-COMPAT ----
