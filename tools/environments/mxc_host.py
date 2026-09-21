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

# Well-known install locations for the MXC kit, in precedence order.
WXC_EXEC_CANDIDATES = (
    r"C:\mxc-kit\bin\wxc-exec.exe",
    r"C:\mxc\wxc-exec.exe",
)

# busybox-w32 (GPLv2, https://frippery.org/busybox/) is the in-sandbox POSIX shell. Pinned to a
# specific release and checksum so provisioning cannot silently pick up a different binary.
BUSYBOX_RELEASE = "FRP-6075-g169694ebd"
BUSYBOX_BASE_URL = "https://frippery.org/files/busybox/"
BUSYBOX_BUILDS = {
    # machine -> (file name on the release server, sha256)
    "arm64": (f"busybox-w64a-{BUSYBOX_RELEASE}.exe",
              "e67f873d19d58c535cc9f0c4965ffd622e19b7bab87e3da89cb2185fb54464d7"),
    "amd64": (f"busybox-w64u-{BUSYBOX_RELEASE}.exe",
              "6e263d154d8548d1eb936f65d1d8312c80df31c45974e48d6335e4dcc0f4f34c"),
}
BUSYBOX_LOCAL_NAME = "busybox-sh.exe"

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
    wxc_exec_path: Optional[str]
    shell_path: Optional[str]
    policy: MxcPolicy
    debug: bool = False
    raw: dict = field(default_factory=dict)


def _clean_paths(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(os.path.expandvars(os.path.expanduser(text)))
    return tuple(out)


def _terminal_section() -> dict:
    """The active profile's ``terminal`` config section (empty on any read failure)."""
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly()
        section = cfg.get("terminal", {}) if isinstance(cfg, dict) else {}
        return section if isinstance(section, dict) else {}
    except Exception:
        logger.debug("mxc: terminal config unavailable", exc_info=True)
        return {}


def resolve_settings(terminal_cfg: Optional[dict] = None) -> MxcSettings:
    """Sandbox settings from config.yaml, read fresh so a policy change applies to the
    next command without restarting anything. Env-bridged ``TERMINAL_MXC_*`` values are
    the fallback for processes launched with only the env bridge."""
    cfg = terminal_cfg if terminal_cfg is not None else _terminal_section()

    def pick(key: str, env_name: str, default: Any) -> Any:
        # Loaded config carries every default key, so an empty value means "not configured"
        # and the env bridge is consulted; an explicit False/list still wins over the env.
        value = cfg.get(key)
        if value is not None and value != "" and value != []:
            return value
        raw = os.environ.get(env_name)
        if raw is None:
            return value if value is not None else default
        if isinstance(default, (list, dict, bool)):
            try:
                return json.loads(raw)
            except ValueError:
                return raw if not isinstance(default, bool) else raw.strip().lower() in ("1", "true", "yes", "on")
        return raw

    wxc = str(pick("mxc_wxc_exec_path", "TERMINAL_MXC_WXC_EXEC_PATH", "") or "").strip() or None
    shell = str(pick("mxc_shell_path", "TERMINAL_MXC_SHELL_PATH", "") or "").strip() or None
    network = pick("mxc_network", "TERMINAL_MXC_NETWORK", False)
    if isinstance(network, str):
        network = network.strip().lower() in ("1", "true", "yes", "on")
    policy = MxcPolicy(
        readwrite_paths=_clean_paths(pick("mxc_readwrite_paths", "TERMINAL_MXC_READWRITE_PATHS", [])),
        readonly_paths=_clean_paths(pick("mxc_readonly_paths", "TERMINAL_MXC_READONLY_PATHS", [])),
        network=bool(network))
    debug = pick("mxc_debug", "TERMINAL_MXC_DEBUG", False)
    if isinstance(debug, str):
        debug = debug.strip().lower() in ("1", "true", "yes", "on")
    return MxcSettings(wxc_exec_path=wxc, shell_path=shell, policy=policy, debug=bool(debug), raw=dict(cfg))


# ── wxc-exec discovery and probe ─────────────────────────────────────────────

def find_wxc_exec(configured: Optional[str] = None) -> Optional[str]:
    """Absolute path of ``wxc-exec.exe`` or None. Order: explicit config, PATH, well-known dirs."""
    if configured:
        candidate = os.path.expandvars(os.path.expanduser(configured))
        return candidate if os.path.isfile(candidate) else None
    found = shutil.which("wxc-exec.exe") or shutil.which("wxc-exec")
    if found:
        return found
    for candidate in WXC_EXEC_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


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
    ok = bool(tier) and tier != "none" and bool(probes.get("baseContainerApiPresent") or tier)
    return {"ok": ok, "tier": tier, "warnings": list(data.get("warnings") or []), "probes": probes, "error": None}


def clear_probe_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()


# ── shell provisioning ───────────────────────────────────────────────────────

def _machine_key() -> Optional[str]:
    machine = (platform.machine() or "").lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("amd64", "x86_64"):
        return "amd64"
    return None


def _host_machine_key() -> Optional[str]:
    """The OS architecture, not the interpreter's: Hermes may run as an emulated x64 process on
    an ARM64 host, and the sandbox shell must match the OS."""
    if _IS_WINDOWS:
        try:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            process_machine = ctypes.c_ushort()
            native_machine = ctypes.c_ushort()
            handle = ctypes.c_void_p(kernel32.GetCurrentProcess())
            if kernel32.IsWow64Process2(handle, ctypes.byref(process_machine), ctypes.byref(native_machine)):
                return {0xAA64: "arm64", 0x8664: "amd64"}.get(native_machine.value)
        except Exception:
            logger.debug("mxc: IsWow64Process2 unavailable", exc_info=True)
    return _machine_key()


def default_shell_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "bin" / BUSYBOX_LOCAL_NAME


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_shell(configured: Optional[str] = None, *, download: bool = True) -> tuple[Optional[str], Optional[str]]:
    """``(shell_path, error)``: the POSIX shell binary for the sandbox.

    A configured path is used as-is. Otherwise the pinned busybox-w32 build for the host
    architecture is expected under ``$HERMES_HOME/bin`` and downloaded there (checksum
    verified, written atomically) when missing and *download* is allowed.
    """
    if configured:
        candidate = os.path.expandvars(os.path.expanduser(configured))
        if os.path.isfile(candidate):
            return candidate, None
        return None, f"terminal.mxc_shell_path does not exist: {configured}"
    target = default_shell_path()
    if target.is_file():
        return str(target), None
    key = _host_machine_key()
    if key not in BUSYBOX_BUILDS:
        return None, f"no pinned busybox-w32 build for this architecture ({platform.machine() or 'unknown'}); set terminal.mxc_shell_path"
    if not download:
        return None, "sandbox shell (busybox-w32) is not installed yet"
    name, expected = BUSYBOX_BUILDS[key]
    url = BUSYBOX_BASE_URL + name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".download")
        with urllib.request.urlopen(url, timeout=60) as response, open(tmp, "wb") as out:  # noqa: S310 - pinned https URL
            shutil.copyfileobj(response, out)
        actual = _sha256(tmp)
        if actual != expected:
            tmp.unlink(missing_ok=True)
            return None, f"downloaded {name} failed checksum verification"
        os.replace(tmp, target)
    except Exception as exc:  # network down, disk full, ...
        return None, f"could not download the sandbox shell ({name}): {exc}"
    logger.info("mxc: installed sandbox shell %s -> %s", name, target)
    return str(target), None


# ── workspace ancestors ──────────────────────────────────────────────────────
#
# A container may only traverse to its granted folders when every ancestor directory is at least
# discoverable. Python and cmd tolerate unreadable ancestors, but Git for Windows resolves the
# working directory component by component (a directory query on each ancestor) and fails with
# "Permission denied" otherwise. The fix is the same one MXC's host prep applies to the drive root:
# a non-inheriting ACE for the AppContainer SIDs on each ancestor that grants directory listing and
# attribute reads only. File contents below those folders stay unreadable.

_APPCONTAINER_SIDS = ("*S-1-15-2-1", "*S-1-15-2-2")  # ALL APPLICATION PACKAGES, ALL RESTRICTED APPLICATION PACKAGES
_ANCESTOR_RIGHTS = "(RD,RA,REA,RC,S)"
_ANCESTOR_RIGHTS_OK = {"RD", "R", "RX", "M", "F"}  # icacls tokens that include directory listing


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


def _icacls(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(["icacls", *args], capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace", **_hidden_window_kwargs())


def _icacls_entries(directory: str) -> Optional[list[tuple[str, set[str]]]]:
    """``(trustee, rights tokens)`` per ACE line of ``icacls <directory>``, or None when unreadable."""
    try:
        completed = _icacls(directory)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    entries: list[tuple[str, set[str]]] = []
    for raw in completed.stdout.splitlines():
        line = raw.strip()
        if ":(" not in line:
            continue
        # The first line carries the path before the first ACE; later lines are indented ACEs.
        if line.lower().startswith(os.path.normpath(directory).lower()):
            line = line[len(os.path.normpath(directory)):].strip()
        trustee, _, rights = line.rpartition(":")
        tokens = {tok.strip().upper() for group in rights.split(")") for tok in group.strip("(").split(",") if tok.strip()}
        entries.append((trustee.strip(), tokens))
    return entries


def _has_appcontainer_listing(entries: list[tuple[str, set[str]]]) -> bool:
    return any(("ALL APPLICATION PACKAGES" in trustee.upper() or "S-1-15-2-1" in trustee) and tokens & _ANCESTOR_RIGHTS_OK
               for trustee, tokens in entries)


def _user_can_modify_acl(entries: list[tuple[str, set[str]]]) -> bool:
    """True when the current user holds Full Control on the folder (Full includes the right to change
    its permissions); Modify does not, so system folders such as the drive root and C:\\Users report
    False and are left to an administrator."""
    user = (os.environ.get("USERNAME") or "").strip().lower()
    if not user:
        return False
    return any(trustee.lower().endswith("\\" + user) and "F" in tokens for trustee, tokens in entries)


def ancestor_ready(directory: str) -> Optional[bool]:
    """Whether *directory* already grants AppContainer processes listing rights (None if unreadable)."""
    entries = _icacls_entries(directory)
    if entries is None:
        return None
    return _has_appcontainer_listing(entries)


def ancestor_readiness(path: str) -> dict:
    """Readiness of *path*'s ancestors for container traversal, in the shape the desktop panel
    consumes: ``ready``, the ``missing`` ancestors, the subset that ``needs_admin`` (the current user
    cannot change their permissions), and the ``admin_command`` that prepares those."""
    missing: list[str] = []
    needs_admin: list[str] = []
    for directory in workspace_ancestors(path):
        entries = _icacls_entries(directory)
        if entries is None or _has_appcontainer_listing(entries):
            continue
        missing.append(directory)
        if not _user_can_modify_acl(entries):
            needs_admin.append(directory)
    return {"ready": not missing, "missing": missing, "needs_admin": needs_admin,
            "admin_command": admin_prepare_command(needs_admin) if needs_admin else ""}


def prepare_ancestors(path: str) -> dict:
    """Add the listing/attributes ACE to every ancestor of *path* that lacks it, without elevation.
    Returns ``{"prepared": [...], "needs_admin": [...], "errors": {dir: message}}``; directories the
    current user may not modify (typically the drive root and ``C:\\Users``) land in ``needs_admin``
    together with the exact command an administrator can run."""
    prepared: list[str] = []
    needs_admin: list[str] = []
    errors: dict[str, str] = {}
    for directory in ancestor_readiness(path)["missing"]:
        grants = [f"{sid}:{_ANCESTOR_RIGHTS}" for sid in _APPCONTAINER_SIDS]
        try:
            completed = _icacls(directory, "/grant", *grants)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors[directory] = str(exc)
            continue
        if completed.returncode == 0 and ancestor_ready(directory):
            prepared.append(directory)
        else:
            needs_admin.append(directory)
    return {"prepared": prepared, "needs_admin": needs_admin, "errors": errors,
            "admin_command": admin_prepare_command(needs_admin) if needs_admin else ""}


def admin_prepare_command(directories: Iterable[str]) -> str:
    """One elevated-prompt command line that prepares *directories*."""
    grants = " ".join(f'"{sid}:{_ANCESTOR_RIGHTS}"' for sid in _APPCONTAINER_SIDS)
    return " && ".join(f'icacls "{d}" /grant {grants}' for d in directories)


def unsafe_workspace_reason(workspace: str) -> Optional[str]:
    """Why *workspace* must not become a sandbox's read/write root, or None when it is fine.

    A grant covers everything beneath the folder, so the drive root, the user profile, any
    folder that contains Hermes's own home (config, credentials, sessions) and any folder that
    contains Hermes's own program files would hand the sandbox the very things it exists to
    protect: the user's data, and the code enforcing the policy."""
    root = os.path.normpath(workspace)
    drive, tail = os.path.splitdrive(root)
    if tail in ("\\", "/", ""):
        return f"The sandbox workspace would be the drive root ({root}). Point terminal.cwd at a project folder."
    home = os.path.normpath(os.path.expanduser("~"))
    if root.lower() == home.lower():
        return (f"The sandbox workspace would be your home folder ({root}), which would grant the sandbox "
                "read/write access to everything in your profile. Point terminal.cwd at a project folder.")
    try:
        from hermes_constants import get_hermes_home
        hermes_home = os.path.normpath(str(get_hermes_home()))
    except Exception:
        hermes_home = ""
    if hermes_home and (hermes_home.lower() + os.sep).startswith(root.lower().rstrip(os.sep) + os.sep):
        return (f"The sandbox workspace ({root}) contains Hermes's own data directory ({hermes_home}), including "
                "credentials. Point terminal.cwd at a project folder.")
    install = os.path.normpath(str(Path(__file__).resolve().parents[2]))
    if (install.lower() + os.sep).startswith(root.lower().rstrip(os.sep) + os.sep):
        return (f"The sandbox workspace ({root}) contains Hermes's own program files ({install}); a sandboxed "
                "agent must not be able to rewrite them. Work in a separate clone, or turn the sandbox off.")
    return None


# Sessions that have no project folder would otherwise be anchored at the user's home, which the
# sandbox refuses. They get a dedicated folder inside the profile instead: rooted where the user
# expects their work to live, without granting Documents, AppData or credentials.
DEFAULT_WORKSPACE_DIRNAME = "Hermes"


def default_workspace() -> str:
    """The folder a sandboxed session without a project works in (created on first use)."""
    target = Path(os.path.expanduser("~")) / DEFAULT_WORKSPACE_DIRNAME
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


def sandbox_workspace_for(cwd: str) -> str:
    """*cwd* when it may be a sandbox workspace, else the default workspace. The one rule every
    surface (session creation, the desktop's default folder, the Sandbox panel, the environment
    itself) applies, so they agree on where a sandboxed session works. An empty or relative *cwd*
    means the process's own directory, which is judged as the folder it resolves to."""
    resolved = os.path.abspath(os.path.expanduser(cwd)) if cwd else os.getcwd()
    return resolved if unsafe_workspace_reason(resolved) is None else default_workspace()


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
    return [str(Path(user_data) / name) for name in ATTACHMENT_STAGING_SUBDIRS if (Path(user_data) / name).is_dir()]


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
    """Whether ``terminal.backend`` selects this backend (config first, env bridge as fallback)."""
    cfg = terminal_cfg if terminal_cfg is not None else _terminal_section()
    backend = cfg.get("backend") if "backend" in cfg else os.environ.get("TERMINAL_ENV")
    return str(backend or "").strip().lower() == "mxc"


# Toolsets that reach the network from the Hermes process itself rather than from inside a
# container. With the sandbox on and its network off, the switch has to mean "the agent is
# offline", so calls to these are refused too; an egress the sandbox cannot see would
# otherwise make the setting a formality.
HOST_NETWORK_TOOLSETS = ("web", "browser")


def host_network_withheld_toolsets(terminal_cfg: Optional[dict] = None) -> tuple[str, ...]:
    """Toolsets whose calls are refused under the current sandbox policy: the host-network
    toolsets when the sandbox is on and its network is off, otherwise nothing."""
    cfg = terminal_cfg if terminal_cfg is not None else _terminal_section()
    if not backend_enabled(cfg):
        return ()
    return () if resolve_settings(cfg).policy.network else HOST_NETWORK_TOOLSETS


OFFLINE_REASON = ("Network is off in the sandbox policy, so Hermes will not fetch URLs on the agent's behalf "
                  "(Hermes desktop: Settings > Safety > Sandbox > Allow network access).")

_offline_cache_lock = threading.Lock()
_offline_cache: tuple[Optional[tuple], bool] = (None, False)


def host_network_withheld() -> bool:
    """Whether the sandbox policy currently keeps the agent offline (sandbox on, network off).

    Consulted per URL by the shared website gate, so the verdict is cached on the config file's
    identity and re-read only when the file changes."""
    global _offline_cache
    if not _IS_WINDOWS:
        return False
    try:
        from hermes_cli.config import get_config_path
        stat = os.stat(get_config_path())
        signature: Optional[tuple] = (str(get_config_path()), stat.st_mtime_ns, stat.st_size)
    except (OSError, ImportError):
        signature = None
    with _offline_cache_lock:
        cached_signature, cached_value = _offline_cache
        if signature is not None and cached_signature == signature:
            return cached_value
    value = bool(host_network_withheld_toolsets())
    with _offline_cache_lock:
        _offline_cache = (signature, value)
    return value


def status(*, provision_shell: bool = False, settings: Optional[MxcSettings] = None) -> dict:
    """One record describing whether the MXC backend can run here and how it is configured.

    ``available`` means every prerequisite holds. ``degraded`` means MXC works but selected the
    AppContainer+DACL fallback tier and reported host-prep warnings. ``reason`` is the first
    blocker in plain language, or None.
    """
    settings = settings or resolve_settings()
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
    if not _IS_WINDOWS:
        record["reason"] = "MXC sandboxing is a Windows feature; this host is not Windows."
        return record
    wxc = find_wxc_exec(settings.wxc_exec_path)
    record["wxc_exec_path"] = wxc
    if wxc is None:
        hint = settings.wxc_exec_path or ", ".join(WXC_EXEC_CANDIDATES)
        record["reason"] = f"wxc-exec.exe (the MXC kit) was not found at {hint}. Install MXC or set terminal.mxc_wxc_exec_path."
        return record
    probe = run_probe(wxc)
    record["probe"] = probe
    record["tier"] = probe.get("tier")
    if not probe["ok"]:
        record["reason"] = ("This Windows build does not support MXC process containers"
                            + (f": {probe['error']}" if probe.get("error") else "."))
        return record
    shell, shell_error = ensure_shell(settings.shell_path, download=provision_shell)
    record["shell_path"] = shell
    if shell is None:
        record["reason"] = shell_error
        record["shell_missing"] = True
        return record
    record["warnings"] = list(probe.get("warnings") or [])
    record["degraded"] = probe.get("tier") == "appcontainer-dacl" and bool(record["warnings"])
    record["available"] = True
    return record


def unavailable_reason() -> Optional[str]:
    """Plain-language reason the backend cannot run here, or None when it can (shell may still
    need provisioning, which the environment does on first use)."""
    record = status(provision_shell=False)
    if record["available"] or record.get("shell_missing"):
        return None
    return record["reason"]
