"""Host-side support for the Windows MXC terminal backend.

Everything here runs on the Hermes host, outside any sandbox: locating ``wxc-exec.exe``
(Microsoft's MXC launcher), probing what the OS can enforce, provisioning the POSIX shell
the sandbox runs (busybox-w32; Git-Bash's MSYS runtime cannot initialize inside an
AppContainer), reading the sandbox policy from config, and summarizing all of it as one
status record that the CLI, ``hermes doctor`` and the desktop share.

``tools.environments.mxc`` (the environment class) consumes these helpers; nothing in
this module depends on ``BaseEnvironment``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# Probing is cheap, but every status read would otherwise spawn a process; verdicts change only
# when the host does (kit installed, host prep run), so a short cache is safe.
_PROBE_TTL_SECONDS = 60.0
_probe_lock = threading.Lock()
_probe_cache: dict[str, tuple[float, dict]] = {}

# One-shot containers launched by this process; the desktop shows it as a running tally.
_container_counter_lock = threading.Lock()
_containers_started = 0


def note_container_started() -> int:
    global _containers_started
    with _container_counter_lock:
        _containers_started += 1
        return _containers_started


def containers_started() -> int:
    with _container_counter_lock:
        return _containers_started


# ── configuration ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MxcPolicy:
    """What the sandbox may touch beyond the task's own working directory."""
    readwrite_paths: tuple[str, ...] = ()
    readonly_paths: tuple[str, ...] = ()
    network: bool = False

    def as_dict(self) -> dict:
        return {"readwrite_paths": list(self.readwrite_paths),
                "readonly_paths": list(self.readonly_paths),
                "network": self.network}


@dataclass(frozen=True)
class MxcSettings:
    policy: MxcPolicy
    debug: bool = False
    raw: dict = field(default_factory=dict)


def _terminal_section() -> dict:
    """The same live, strict authority used by terminal execution."""
    from tools.terminal_scope import get_live_terminal_config
    return get_live_terminal_config()


def resolve_settings(terminal_cfg: Optional[dict] = None) -> MxcSettings:
    """Live profile settings, or an explicitly supplied authoritative configuration.

    The kit and the shell come from the pm store, so this reads only policy.
    """
    cfg = terminal_cfg if terminal_cfg is not None else _terminal_section()
    if not isinstance(cfg, dict):
        raise ValueError("Terminal policy must be a mapping")
    for key in ("mxc_network", "mxc_debug"):
        if key in cfg and not isinstance(cfg[key], bool):
            raise ValueError(f"terminal.{key} must be a boolean")
    network = cfg.get("mxc_network", False)
    policy = MxcPolicy(
        readwrite_paths=tuple(validate_grant_paths(cfg.get("mxc_readwrite_paths", []), writable=True)),
        readonly_paths=tuple(validate_grant_paths(cfg.get("mxc_readonly_paths", []), writable=False)),
        network=bool(network))
    return MxcSettings(policy=policy, debug=cfg.get("mxc_debug", False), raw=dict(cfg))


# ── wxc-exec discovery and probe ─────────────────────────────────────────────

def store_binary(name: str):
    """The pm store's binary for *name*, or None when this install has no copy.

    The store is the only source for the kit and the shell. A sealed install
    carries them in the payload store, which is read first; a source install
    and a toggle provision land in the writable store, which is the fallback.
    A hand copy on PATH or under ``C:\\mxc-kit\\bin`` is not consulted.
    """
    try:
        from pm.paths import lockfile_path, store_root, writable_store_root
        from pm.registry import get_package
        from pm.store import current_target
        from pm.lock import Lockfile

        package = get_package(name)
        target = current_target()
        version = Lockfile(lockfile_path()).version(name)
        if not version or package.missing_reason(target):
            return None
        for root in dict.fromkeys((store_root(), writable_store_root())):
            binary = package.binary(root / package.store_entry(version, target), target)
            if binary is not None and binary.is_file():
                return binary
    except Exception:
        logger.debug("mxc: pm store lookup for %s failed", name, exc_info=True)
        return None
    return None


def find_wxc_exec(configured: Optional[str] = None) -> Optional[str]:
    """Absolute path of ``wxc-exec.exe`` from the pm store, or None.

    *configured* is accepted and ignored: the config key that used to name a
    hand-installed kit is gone, and the store is the only source.
    """
    del configured
    binary = store_binary("mxc-kit")
    return str(binary) if binary is not None else None


def run_probe(wxc_exec: str, *, timeout: float = 15.0) -> dict:
    """``wxc-exec --probe`` decoded, cached for a short TTL per binary path.

    Returns ``{"ok": bool, "tier": str|None, "warnings": [...], "probes": {...}, "error": str|None}``.
    """
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get(wxc_exec)
        if cached and now - cached[0] < _PROBE_TTL_SECONDS:
            return dict(cached[1])
    result = _run_probe_uncached(wxc_exec, timeout=timeout)
    with _probe_lock:
        _probe_cache[wxc_exec] = (now, result)
    return dict(result)


def _run_probe_uncached(wxc_exec: str, *, timeout: float) -> dict:
    try:
        completed = subprocess.run(
            [wxc_exec, "--probe"], capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", **_hidden_window_kwargs())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "tier": None, "warnings": [], "probes": {}, "error": f"could not run wxc-exec --probe: {exc}"}
    text = (completed.stdout or "").strip()
    start = text.find("{")
    if completed.returncode != 0 or start == -1:
        detail = (completed.stderr or text or f"exit {completed.returncode}").strip()
        return {"ok": False, "tier": None, "warnings": [], "probes": {}, "error": detail[:500]}
    try:
        data = json.loads(text[start:])
    except ValueError as exc:
        return {"ok": False, "tier": None, "warnings": [], "probes": {}, "error": f"unreadable probe output: {exc}"}
    tier = data.get("tier")
    probes = data.get("probes") or {}
    result = {"ok": True, "tier": tier, "warnings": list(data.get("warnings") or []), "probes": probes, "error": None}
    result["error"] = strict_probe_reason(result)
    result["ok"] = result["error"] is None
    return result


_REQUIRED_UI_CAPABILITIES = (
    "canBlockClipboardRead", "canBlockClipboardWrite", "canBlockInputInjection",
    "canBlockInputMethodChanges", "canBlockExternalUiObjects", "canBlockGlobalUiNamespace",
    "canBlockDesktopSwitching", "canBlockLogoffOrShutdown",
    "canBlockSystemParameterChanges", "canBlockDisplaySettingsChanges",
)


def strict_probe_reason(probe: dict) -> Optional[str]:
    """A fallback tier is not the strict filesystem/UI boundary Hermes advertises."""
    if not probe.get("ok"):
        return probe.get("error") or "MXC could not verify this host's isolation capabilities."
    facts = probe.get("probes") or {}
    if probe.get("tier") != "base-container" or facts.get("baseContainerApiPresent") is not True:
        return "MXC requires the base-container tier; filesystem/DACL fallback is disabled."
    ui = facts.get("uiCapabilities") or {}
    missing = [name for name in _REQUIRED_UI_CAPABILITIES if ui.get(name) is not True]
    if missing:
        return "MXC cannot enforce the required UI isolation: " + ", ".join(missing)
    return None


def clear_probe_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()


# ── shell provisioning ───────────────────────────────────────────────────────

def ensure_shell(configured: Optional[str] = None, *, download: bool = True) -> tuple[Optional[str], Optional[str]]:
    """``(shell_path, error)``: the POSIX shell binary for the sandbox.

    The shell is the pinned busybox-w32 in the pm store. Nothing is downloaded
    here — the sandbox toggle provisions the pin, and a missing copy is reported
    as missing so the toggle can show the error instead of a half-installed
    backend. *configured* and *download* are accepted and ignored.
    """
    del configured, download
    binary = store_binary("busybox")
    if binary is not None:
        return str(binary), None
    return None, "sandbox shell (busybox-w32) is not installed yet"


# ── workspace ancestors ──────────────────────────────────────────────────────
#
# Ancestor ACL preparation is retired. Keep a refusal for callers of the old API;
# Hermes must not alter global AppContainer access as a command-side workaround.


def workspace_ancestors(path: str) -> list[str]:
    """Ancestor directories of *path* from the drive root down, excluding *path* itself."""
    current = os.path.normpath(os.path.abspath(path))
    ancestors: list[str] = []
    while True:
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        ancestors.append(parent)
        current = parent
    return list(reversed(ancestors))


def ancestor_ready(directory: str) -> Optional[bool]:
    """ACL traversal readiness is no longer inferred from locale-dependent listings."""
    return None


def ancestor_readiness(path: str) -> dict:
    """Compatibility status: unknown, never a claim that host ACLs are prepared."""
    return {"ready": False, "missing": [], "needs_admin": [], "admin_command": "",
            "error": "Ancestor ACL preparation is disabled; traversal readiness is unknown."}


def prepare_ancestors(path: str) -> dict:
    """Retired endpoint: never mutate host ACLs."""
    raise RuntimeError("Ancestor ACL preparation is disabled. See the Windows sandbox documentation for traversal limitations.")


def admin_prepare_command(directories: Iterable[str]) -> str:
    """No administrator command is generated for the retired ACL workflow."""
    return ""


def _canonical_path(path: str, *, strict: bool = True) -> str:
    """Resolve filesystem aliases before comparing or publishing authority."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise ValueError("A grant must name an existing absolute path")
    expanded = os.path.expandvars(os.path.expanduser(path.strip()))
    if not os.path.isabs(expanded):
        raise ValueError(f"A grant must be absolute: {path}")
    try:
        resolved = str(Path(expanded).resolve(strict=strict))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"Cannot resolve grant path {path}: {exc}") from exc
    # realpath resolves junctions and 8.3 aliases; strip the Win32 extended prefix
    # only afterwards so the identity comparison uses one spelling.
    if resolved.startswith("\\\\?\\UNC\\"):
        resolved = "\\\\" + resolved[8:]
    elif resolved.startswith("\\\\?\\"):
        resolved = resolved[4:]
    return os.path.normpath(resolved)


def _overlaps(left: str, right: str) -> bool:
    left, right = os.path.normcase(left), os.path.normcase(right)
    try:
        return os.path.commonpath([left, right]) in (left, right)
    except ValueError:
        return False


def _trusted_runtime_path(path: str) -> str:
    """Internal exceptions cannot be redirected into another protected subtree."""
    canonical = _canonical_path(path)
    if (os.path.normcase(canonical) != os.path.normcase(os.path.abspath(path))
            and _protected_path_reason(canonical)):
        raise ValueError(f"Internal runtime path is an alias, not an authorized protected exception: {path}")
    return canonical


def _protected_path_reason(path: str) -> Optional[str]:
    home = _canonical_path(os.path.expanduser("~"), strict=False)
    if os.path.dirname(path) == path:
        return f"The sandbox grant would be the drive root ({path})."
    if os.path.normcase(path) == os.path.normcase(home):
        return f"The sandbox grant would be your home folder ({path})."
    install = _canonical_path(str(Path(__file__).resolve().parents[2]))
    if _overlaps(path, install):
        return f"The sandbox grant overlaps Hermes's own program files ({install}). Work in a separate clone."
    for canonical in _protected_data_roots():
        if _overlaps(path, canonical):
            return f"The sandbox grant overlaps Hermes's own data directory ({canonical}), including credentials."
    user_data = os.environ.get("HERMES_DESKTOP_USER_DATA")
    if user_data and _overlaps(path, _canonical_path(user_data, strict=False)):
        return "The sandbox grant overlaps protected desktop data and credentials."
    return None


def _protected_data_roots() -> list[str]:
    from hermes_constants import (get_hermes_home, get_process_hermes_home, get_default_hermes_root,
                                  _get_platform_default_hermes_home)
    return [_canonical_path(str(root), strict=False) for root in
            (get_hermes_home(), get_process_hermes_home(), get_default_hermes_root(),
             _get_platform_default_hermes_home())]


def _shell_runtime_path(shell: str) -> str:
    # A configured shell grants the executable only, never an arbitrary protected
    # descendant disguised as a runtime. The pinned shell in the pm store is an
    # explicit exception to user-grant validation, checked for reparse redirection
    # as well.
    shell = _trusted_runtime_path(shell)
    if os.path.normcase(shell) in _store_shell_paths():
        return shell
    return validate_grant_paths([shell], writable=False)[0]


def _store_shell_paths() -> set[str]:
    """Every pinned busybox copy the store may hold, payload and writable."""
    try:
        from pm.paths import lockfile_path, store_root, writable_store_root
        from pm.registry import get_package
        from pm.store import current_target
        from pm.lock import Lockfile

        package = get_package("busybox")
        target = current_target()
        version = Lockfile(lockfile_path()).version("busybox")
        if not version or package.missing_reason(target):
            return set()
        return {os.path.normcase(str(root / package.store_entry(version, target) / "busybox-sh.exe"))
                for root in dict.fromkeys((store_root(), writable_store_root()))}
    except Exception:
        return set()


def validate_grant_paths(paths, *, writable: bool) -> list[str]:
    """Validate user authority, never internal runtime/scratch exceptions.

    Both read and write grants exclude protected roots and their ancestors and
    descendants. Return canonical existing paths; malformed entries fail closed.
    """
    if not isinstance(paths, (list, tuple)):
        raise ValueError("Sandbox grant paths must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = _canonical_path(raw)
        reason = _protected_path_reason(path)
        if reason:
            raise ValueError(reason)
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def unsafe_workspace_reason(workspace: str) -> Optional[str]:
    """Canonical protected overlap check shared by workspace and user grant selection."""
    try:
        return _protected_path_reason(_canonical_path(workspace, strict=False))
    except ValueError as exc:
        return str(exc)


# Sessions that have no project folder would otherwise be anchored at the user's home, which the
# sandbox refuses. They get a dedicated folder inside the profile instead: rooted where the user
# expects their work to live, without granting Documents, AppData or credentials.
DEFAULT_WORKSPACE_DIRNAME = "Hermes"


def default_workspace() -> str:
    """The folder a sandboxed session without a project works in (created on first use)."""
    target = Path(os.path.expanduser("~")) / DEFAULT_WORKSPACE_DIRNAME
    reason = unsafe_workspace_reason(str(target))
    if reason:
        raise ValueError(reason)
    target.mkdir(parents=True, exist_ok=True)
    return validate_grant_paths([str(target)], writable=True)[0]


def sandbox_workspace_for(cwd: str) -> str:
    """*cwd* when it may be a sandbox workspace, else the default workspace. The one rule every
    surface (session creation, the desktop's default folder, the Sandbox panel, the environment
    itself) applies, so they agree on where a sandboxed session works. An empty or relative *cwd*
    means the process's own directory, which is judged as the folder it resolves to."""
    resolved = os.path.abspath(os.path.expanduser(cwd)) if cwd else os.getcwd()
    return _canonical_path(resolved, strict=False) if unsafe_workspace_reason(resolved) is None else default_workspace()


# The desktop stages what the user pastes or attaches in the composer under its Electron user-data
# folder. That content was handed to the agent deliberately, so the sandbox may always read it;
# the desktop tells its spawned backend where that folder is. Only the staging subfolders are
# granted: the user-data folder itself holds connection tokens and browser storage.
DESKTOP_USER_DATA_ENV = "HERMES_DESKTOP_USER_DATA"
ATTACHMENT_STAGING_SUBDIRS = ("composer-images", "composer-pastes")


def attachment_staging_dirs() -> list[str]:
    """Existing composer staging folders of the desktop that spawned this backend (empty otherwise)."""
    user_data = (os.environ.get(DESKTOP_USER_DATA_ENV) or "").strip()
    if not user_data:
        return []
    return [_trusted_runtime_path(str(Path(user_data) / name))
            for name in ATTACHMENT_STAGING_SUBDIRS if (Path(user_data) / name).is_dir()]


# ── status ───────────────────────────────────────────────────────────────────

def _os_build() -> Optional[str]:
    if not _IS_WINDOWS:
        return None
    try:
        v = sys.getwindowsversion()  # type: ignore[attr-defined]
        return f"{v.major}.{v.minor}.{v.build}"
    except Exception:
        return None


def _hidden_window_kwargs() -> dict:
    if not _IS_WINDOWS:
        return {}
    from hermes_cli._subprocess_compat import windows_hide_flags
    return {"creationflags": windows_hide_flags()}


def backend_enabled(terminal_cfg: Optional[dict] = None) -> bool:
    """Whether the strict live terminal authority selects this backend."""
    cfg = terminal_cfg if terminal_cfg is not None else _terminal_section()
    backend = cfg.get("backend")
    return str(backend or "").strip().lower() == "mxc"


OFFLINE_REASON = ("Network is off in the sandbox policy, so Hermes will not fetch URLs on the agent's behalf "
                  "(Hermes desktop: Settings > Safety > Sandbox > Allow network access).")


def status(*, provision_shell: bool = False, settings: Optional[MxcSettings] = None) -> dict:
    """One record describing whether the MXC backend can run here and how it is configured.

    ``available`` means every strict prerequisite holds. Fallback tiers are unavailable,
    never silently degraded. ``reason`` is the first
    blocker in plain language, or None.
    """
    policy_error = None
    if settings is None:
        try:
            settings = resolve_settings()
        except Exception as exc:
            policy_error = f"Sandbox policy unavailable: {exc}"
            settings = MxcSettings(None, None, MxcPolicy())
    record: dict[str, Any] = {
        "platform_supported": _IS_WINDOWS,
        "os_build": _os_build(),
        "enabled": backend_enabled(settings.raw),
        "policy": settings.policy.as_dict(),
        "containers_started": containers_started(),
        "wxc_exec_path": None,
        "probe": None,
        "tier": None,
        "shell_path": None,
        "shell_missing": False,
        "available": False,
        "degraded": False,
        "warnings": [],
        "reason": None,
    }
    if policy_error:
        record["reason"] = policy_error
        return record
    if not _IS_WINDOWS:
        record["reason"] = "MXC sandboxing is a Windows feature; this host is not Windows."
        return record
    wxc = find_wxc_exec()
    record["wxc_exec_path"] = wxc
    if wxc is None:
        record["reason"] = "wxc-exec.exe (the MXC kit) is not in the pm store. Flip the sandbox on to provision it, or rebuild this sealed install."
        return record
    probe = run_probe(wxc)
    record["probe"] = probe
    record["tier"] = probe.get("tier")
    reason = strict_probe_reason(probe)
    if reason:
        record["reason"] = reason
        return record
    shell, shell_error = ensure_shell()
    record["shell_path"] = shell
    if shell is None:
        record["reason"] = shell_error
        record["shell_missing"] = True
        return record
    record["warnings"] = list(probe.get("warnings") or [])

    record["available"] = True
    return record


def unavailable_reason() -> Optional[str]:
    """Plain-language reason the backend cannot run here, or None when it can (shell may still
    need provisioning, which the environment does on first use)."""
    record = status(provision_shell=False)
    if record["available"] or record.get("shell_missing"):
        return None
    return record["reason"]
