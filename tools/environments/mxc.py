"""Windows MXC terminal backend: every command runs in a fresh, kernel-enforced AppContainer.

Microsoft's MXC launcher (``wxc-exec.exe``) starts one process inside a process container
whose filesystem view is default-deny: only the paths named in its policy are readable or
writable, and network access is granted or refused as a whole. Hermes hands it one shell
invocation per tool call, so the sandbox policy is re-evaluated on every command and a
change made in settings applies to the very next one, with nothing to restart.

Terminal commands, file tools, search and terminal background processes run here.
Uncontained host execution (including ``execute_code``) is refused by the shared
admission policy. This class deliberately is not a ``LocalEnvironment``: the file
tools' host fast paths must never bypass the container.

The in-sandbox shell is busybox-w32's POSIX ``sh``. Git-Bash cannot be used: its MSYS runtime
opens the session's named-object directory at startup, which an AppContainer denies, so the
session bootstrap and per-command wrapper here are written in plain POSIX sh rather than the
bash dialect the other backends share.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Iterable, Optional

from hermes_cli._subprocess_compat import windows_hide_flags
from tools.environments import mxc_host
from tools.environments.base import BaseEnvironment
from tools.environments.base_output import ProcessHandle, _pipe_stdin
from tools.environments.base_session_env import (_SNAP_TMP, _SNAP_TMP_SUFFIX, _cwd_marker_printf,
                                                 _passthrough_save_restore)

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

MXC_SCHEMA_VERSION = "0.8.0-alpha"

# Environment variables every Win32 process needs to start; copied from the host verbatim.
_HOST_BASE_ENV = ("SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATHEXT", "PROCESSOR_ARCHITECTURE",
                  "NUMBER_OF_PROCESSORS", "USERNAME", "USERDOMAIN", "PROGRAMFILES", "PROGRAMDATA", "OS")
# Per-user locations redirected into the sandbox scratch directory so tools that write to
# ``%LOCALAPPDATA%``/``$HOME`` land in the sandbox, never in the real profile.
_REDIRECTED_HOME_VARS = ("LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "USERPROFILE", "HOME")

# Prefix families of per-session variables that must never persist in the session snapshot
# (mirrors the bash bootstrap's exclusion set in base_session_env).
_SNAPSHOT_EXCLUDED_PREFIXES = ("HERMES_SESSION_", "HERMES_CRON_AUTO_DELIVER_", "HERMES_BROWSER_CONTROL_")
_SNAPSHOT_EXCLUDED_NAMES = ("AI_AGENT", "HERMES_AGENT", "HERMES_DELEGATED_CHILD_CONTEXT",
                            "HERMES_CRON_SESSION", "HERMES_UI_SESSION_ID")


# ── pure helpers (unit-tested) ───────────────────────────────────────────────

def to_forward_slashes(path: str) -> str:
    """``C:\\Users\\x`` -> ``C:/Users/x``: the form busybox-w32 and native tools both accept."""
    return path.replace("\\", "/")


def to_native_path(path: str) -> str:
    """Normalize a sandbox-reported path (``C:/Users/x``) to native Windows form."""
    text = path.strip()
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return os.path.normpath(text)
    return text


def normalize_grant_paths(paths: Iterable[str]) -> list[str]:
    """Absolute, normalized, de-duplicated grant paths. Relative and empty entries are dropped:
    a grant must name a real location, never something that resolves differently per command."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in paths:
        text = str(raw or "").strip()
        if not text:
            continue
        expanded = os.path.expandvars(os.path.expanduser(text))
        if not os.path.isabs(expanded):
            continue
        native = os.path.normpath(expanded)
        key = native.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(native)
    return out


def build_container_config(*, container_id: str, command_line: str, cwd: str, env: dict[str, str],
                           readwrite_paths: Iterable[str], readonly_paths: Iterable[str],
                           network: bool) -> dict:
    """The one-shot ``processcontainer`` request ``wxc-exec`` executes.

    ``ui.disable`` stays false because console tools (cmd, PowerShell, node) touch Win32k
    during startup; clipboard and input injection remain blocked. A network section is always
    present, even when egress is denied, because MXC only wires intra-container loopback when
    one exists. ``process.timeout`` is 0: Hermes owns timeouts and kills the launcher itself.
    """
    readwrite = normalize_grant_paths(readwrite_paths)
    rw_keys = {p.lower() for p in readwrite}
    readonly = [p for p in normalize_grant_paths(readonly_paths) if p.lower() not in rw_keys]
    return {
        "version": MXC_SCHEMA_VERSION,
        "containerId": container_id,
        "containment": "processcontainer",
        "process": {
            "commandLine": command_line,
            "cwd": os.path.normpath(cwd),
            "env": [f"{k}={v}" for k, v in env.items()],
            "timeout": 0,
        },
        "processContainer": {"leastPrivilege": False},
        "fallback": {"allowDaclMutation": False},
        "filesystem": {"readwritePaths": readwrite, "readonlyPaths": readonly},
        "network": {"egress": {"default": "allow" if network else "deny"}},
        "ui": {"disable": False, "clipboard": "none", "injection": False},
    }


def posix_bootstrap_script(*, quoted_cwd: str, quoted_snap: str, snap_tmp_template: str,
                           excluded_names: Iterable[str], cwd_marker: str) -> str:
    """POSIX-sh session bootstrap: capture ``export -p`` into the snapshot (atomic publish),
    restore the configured cwd, emit the cwd marker. Functions and aliases are not captured;
    the sandbox shell starts from a fixed environment, so there is nothing profile-provided
    to preserve."""
    return (
        "umask 077\n"
        f"__hermes_snap_tmp=$(mktemp {snap_tmp_template}) || exit 1\n"
        f"{posix_export_dump(_SNAP_TMP, excluded_names)}\n"
        f"echo 'set +e' >> {_SNAP_TMP}\n"
        f"echo 'set +u' >> {_SNAP_TMP}\n"
        f"mv -f {_SNAP_TMP} {quoted_snap} || rm -f {_SNAP_TMP}\n"
        f"cd -- {quoted_cwd} 2>/dev/null || true\n"
        f"{_cwd_marker_printf(cwd_marker)}\n")


def posix_export_dump(tmp_path: str, excluded_names: Iterable[str] = ()) -> str:
    """``export -p`` to *tmp_path* minus per-session variables, in POSIX sh (no ``${!PREFIX*}``).
    Names are enumerated from ``export -p`` itself and unset in a subshell before the dump."""
    safe_names = {n for n in (*_SNAPSHOT_EXCLUDED_NAMES, *excluded_names) if isinstance(n, str) and n}
    fixed = " ".join(shlex.quote(n) for n in sorted(safe_names))
    prefix_cases = "|".join(f"{p}*" for p in _SNAPSHOT_EXCLUDED_PREFIXES)
    return (
        "{ ( "
        "for __hermes_n in $(export -p | sed -n 's/^export \\([A-Za-z_][A-Za-z0-9_]*\\)=.*/\\1/p'); do "
        f"case \"$__hermes_n\" in {prefix_cases}) unset \"$__hermes_n\";; esac; done; "
        f"unset {fixed} 2>/dev/null; export -p; ) || true; }} > {tmp_path}")


def posix_wrap_command_script(command: str, *, quoted_cwd: str, quoted_snap: str, snap_tmp_template: str,
                              passthrough_names: Iterable[str], snapshot_ready: bool, cwd_marker: str) -> str:
    """Per-command POSIX-sh wrapper: source the snapshot, cd, run, re-dump the environment,
    emit the cwd marker, exit with the command's status."""
    escaped = command.replace("'", "'\\''")
    save, restore = _passthrough_save_restore(passthrough_names)
    parts = list(save)
    if snapshot_ready:
        parts.append(f". {quoted_snap} >/dev/null 2>&1 || true")
    parts += restore
    parts += [
        'export AI_AGENT="${AI_AGENT:-hermes-agent}" HERMES_AGENT="${HERMES_AGENT:-true}"',
        'export GIT_PAGER="${GIT_PAGER:-cat}" PAGER="${PAGER:-cat}"',
        f"cd -- {quoted_cwd} || exit 126",
        f"eval '{escaped}'",
        "__hermes_ec=$?",
        "umask 077",
    ]
    if snapshot_ready:
        parts.append(
            f"__hermes_snap_tmp=$(mktemp {snap_tmp_template}) && "
            f"{{ {posix_export_dump(_SNAP_TMP, passthrough_names)} && mv -f {_SNAP_TMP} {quoted_snap}; }} "
            f"2>/dev/null || rm -f {_SNAP_TMP} 2>/dev/null || true")
    parts += [_cwd_marker_printf(cwd_marker), "exit $__hermes_ec"]
    return "\n".join(parts)


# Denial signatures from the shells and runtimes that commonly run inside the sandbox. Each
# pattern's optional ``path`` group is the location the OS refused.
_WIN_PATH = r"(?:[A-Za-z]:[\\/][^'\"\n]*?|/[^'\"\n]+?)"
_DENIAL_PATTERNS = (
    # Git for Windows cannot resolve the working directory when an ancestor folder is not
    # discoverable by the container; a recursive file grant is not a traversal remedy.
    re.compile(r"fatal: [Uu]nable to (?:get|read) current working directory: Permission denied"),
    re.compile(r"can't (?:create|open|remove|stat|chdir to|cd to|read|write|create directory|move|copy)(?: to)? '?(?P<path>" + _WIN_PATH + r")'?: Permission denied", re.I),
    re.compile(r"PermissionError: \[Errno 13\] Permission denied: '(?P<path>[^']+)'"),
    re.compile(r"\[WinError 5\] Access is denied: '(?P<path>[^']+)'"),
    re.compile(r"E(?:ACCES|PERM): [^\n]*?'(?P<path>[^']+)'"),
    re.compile(r"(?P<path>" + _WIN_PATH + r"): Permission denied", re.I),
    re.compile(r"Permission denied"),
    re.compile(r"Access is denied\.?"),
)


GIT_ANCESTOR_DENIAL = "the workspace's parent folders (git resolves the working directory through them)"


def find_denials(output: str) -> list[str]:
    """Paths (or descriptive markers) the sandbox refused, in order of appearance."""
    found: list[str] = []
    seen_lines: set[str] = set()
    for line in output.splitlines():
        if line in seen_lines:
            continue
        for index, pattern in enumerate(_DENIAL_PATTERNS):
            match = pattern.search(line)
            if not match:
                continue
            seen_lines.add(line)
            if index == 0:
                entry = GIT_ANCESTOR_DENIAL
            else:
                path = (match.groupdict().get("path") or "").strip().rstrip(".,;:")
                entry = to_native_path(path) if path else "a path outside the sandbox policy"
            if entry not in found:
                found.append(entry)
            break
    return found


def denial_note(denied: list[str], *, workspace: str, policy: mxc_host.MxcPolicy) -> str:
    """Plain-language explanation appended to a denied command's output. It names what was
    refused and what is currently allowed so the model can ask for exactly the grant it needs
    instead of guessing or trying to route around the sandbox."""
    writable = [workspace, *policy.readwrite_paths]
    lines = ["[Sandbox] Windows MXC denied access outside the sandbox policy:"]
    lines += [f"  denied: {p}" for p in denied]
    lines.append("  read/write: " + ", ".join(writable))
    lines.append("  read-only: " + (", ".join(policy.readonly_paths) if policy.readonly_paths else "(none)"))
    lines.append("  network: " + ("on" if policy.network else "off"))
    if GIT_ANCESTOR_DENIAL in denied:
        lines.append("Git cannot resolve this workspace through its parent folders. Hermes does not change host "
                     "ACLs to work around this limitation. See the Windows sandbox documentation; an extra "
                     "recursive file-access grant is not a traversal remedy.")
    lines.append("The user controls this policy (Hermes desktop: Settings > Safety > Sandbox). If the task needs "
                 "that location, stop and ask the user to grant access; a grant applies to your next command. "
                 "Do not try to work around the sandbox.")
    return "\n".join(lines)


# ── environment ──────────────────────────────────────────────────────────────

class MxcEnvironment(BaseEnvironment):
    """Run each command inside a one-shot Windows MXC process container."""

    # Not the host: the file tools' native fast paths must stay off (see module docstring).
    is_local = False
    # Lets the terminal tool's cwd sanitizers recognize a live sandbox environment.
    env_type = "mxc"
    # Shell file operations quote paths for the sandbox's POSIX shell, which takes native drive
    # paths with forward slashes (``C:/Users/x``), not the Git Bash ``/c/Users/x`` form.
    windows_path_form = "native"
    _stdin_mode = "pipe"
    _snapshot_timeout = 30
    _sudo_nopasswd_probe_supported = False

    def __init__(self, cwd: str = "", timeout: int = 60, env: Optional[dict] = None, *,
                 settings: Optional[mxc_host.MxcSettings] = None, task_id: str = "default"):
        if not _IS_WINDOWS:
            raise RuntimeError("the mxc terminal backend runs only on Windows")
        from tools.environments.local import _resolve_local_initial_cwd
        self.task_id = task_id
        self._settings_override = settings
        self._script_paths: set[str] = set()
        self._process_lock = threading.RLock()
        self._processes: set[subprocess.Popen] = set()
        self._closed = False
        self._container_seq = 0
        self._git_identity: Optional[dict[str, str]] = None
        super().__init__(cwd=_resolve_local_initial_cwd(cwd), timeout=timeout, env=env)
        # The read/write grant is the session's workspace root, not the live cwd: a ``cd`` into
        # a subdirectory stays covered, and a ``cd`` outside it is refused by the sandbox. A cwd
        # the policy refuses as a workspace (the install tree, home, a drive root) re-homes to the
        # default workspace rather than failing: the sandbox may have been turned on for a
        # session that started elsewhere, and a refusal here would take every tool down with it.
        requested = to_native_path(self.cwd)
        self.workspace_root = mxc_host.sandbox_workspace_for(requested)
        if os.path.normcase(self.workspace_root) != os.path.normcase(requested):
            logger.info("mxc: %s Working in %s instead.",
                        mxc_host.unsafe_workspace_reason(requested), self.workspace_root)
            self.cwd = self.workspace_root
        self._sandbox_home = f"{self.get_temp_dir()}/mxc-home-{self._session_id}"
        os.makedirs(self._sandbox_home, exist_ok=True)
        self._prefer_nonlogin = True  # there is no login-shell concept in the sandbox
        self.init_session()

    # -- settings -------------------------------------------------------------

    def _settings(self) -> mxc_host.MxcSettings:
        """Read fresh per call so a policy edit applies to the next command."""
        return self._settings_override or mxc_host.resolve_settings()

    def _resolve_launcher(self, settings: mxc_host.MxcSettings) -> tuple[str, str]:
        record = mxc_host.status(settings=settings)
        if not record["available"]:
            raise RuntimeError(record["reason"])
        return record["wxc_exec_path"], record["shell_path"]

    # -- paths ----------------------------------------------------------------

    def get_temp_dir(self) -> str:
        """Private per-environment scratch, allocated before BaseEnvironment names its snapshot.

        Neither the shared parent nor LocalEnvironment's terminal cache is granted.
        Keep the selected directory pinned even if the calling profile changes.
        """
        if not hasattr(self, "_scratch_dir"):
            from hermes_constants import get_hermes_home
            parent = get_hermes_home().resolve() / "cache" / "mxc"
            parent.mkdir(parents=True, exist_ok=True)
            if parent.resolve() != parent:
                raise RuntimeError("MXC scratch parent has been redirected; refusing host script creation.")
            self._scratch_dir = tempfile.mkdtemp(prefix="session-", dir=parent)
            stat = os.stat(self._scratch_dir)
            self._scratch_identity = (stat.st_dev, stat.st_ino)
        try:
            stat = os.stat(self._scratch_dir)
            if ((stat.st_dev, stat.st_ino) != self._scratch_identity
                    or Path(self._scratch_dir).resolve() != Path(self._scratch_dir)):
                raise RuntimeError("MXC scratch directory has been replaced; refusing host script creation.")
        except OSError as exc:
            raise RuntimeError("MXC scratch directory is unavailable.") from exc
        return to_forward_slashes(self._scratch_dir)

    @staticmethod
    def _quote_cwd_for_cd(cwd: str) -> str:
        return shlex.quote(to_forward_slashes(cwd))

    def _quote_shell_path(self, path: str) -> str:
        return shlex.quote(to_forward_slashes(path))

    def _extract_cwd_from_output(self, result: dict):
        prev_cwd = self.cwd
        super()._extract_cwd_from_output(result)
        if self.cwd != prev_cwd:
            native = to_native_path(self.cwd)
            if native and os.path.isdir(native):
                self.cwd = native
                result["cwd"] = native
            else:
                self.cwd = prev_cwd
                result.pop("cwd_observed", None)
                result.pop("cwd", None)

    # -- grants ---------------------------------------------------------------

    def _tool_readonly_grants(self, shell: str) -> list[str]:
        """Directories the sandbox must read for Hermes-managed tools to launch: the shell, this
        install tree, the interpreter the venv trampolines into, managed node/git runtimes."""
        from hermes_constants import get_hermes_home, iter_hermes_node_dirs
        grants: list[str] = [mxc_host._shell_runtime_path(shell), str(Path(__file__).resolve().parents[2])]
        venv_cfg = Path(sys.prefix) / "pyvenv.cfg"
        if venv_cfg.is_file():
            for line in venv_cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() == "home" and value.strip():
                    grants.append(value.strip())
        else:
            grants.append(sys.prefix)
        home = get_hermes_home()
        grants += [str(d) for d in iter_hermes_node_dirs() if d.is_dir()]
        grants += [str(home / name) for name in ("git", "bin") if (home / name).is_dir()]
        grants += mxc_host.attachment_staging_dirs()
        return [mxc_host._trusted_runtime_path(p) for p in grants]

    def _tool_path_entries(self, shell: str) -> list[str]:
        from hermes_constants import get_hermes_home, iter_hermes_node_dirs
        home = get_hermes_home()
        entries = [os.path.dirname(shell), os.path.join(sys.prefix, "Scripts")]
        entries += [str(d) for d in iter_hermes_node_dirs() if d.is_dir()]
        git_cmd = home / "git" / "cmd"
        if git_cmd.is_dir():
            entries.append(str(git_cmd))
        if (home / "bin").is_dir():
            entries.append(str(home / "bin"))
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        entries += [os.path.join(system_root, "System32"), system_root,
                    os.path.join(system_root, "System32", "Wbem"),
                    os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0")]
        return [e for e in entries if e]

    def _git_identity_env(self) -> dict[str, str]:
        """The host user's git identity, exported so commits inside the sandbox work even though
        the real profile (and its .gitconfig) is not readable there."""
        if self._git_identity is None:
            identity: dict[str, str] = {}
            git = shutil.which("git")
            if git:
                for key, var in (("user.name", "NAME"), ("user.email", "EMAIL")):
                    with contextlib.suppress(Exception):
                        value = subprocess.run([git, "config", "--get", key], capture_output=True, text=True,
                                               timeout=5, creationflags=windows_hide_flags()).stdout.strip()
                        if value:
                            identity[f"GIT_AUTHOR_{var}"] = value
                            identity[f"GIT_COMMITTER_{var}"] = value
            self._git_identity = identity
        return dict(self._git_identity)

    def build_sandbox_env(self, shell: str) -> dict[str, str]:
        """The complete environment of the sandboxed process. Built from scratch rather than
        inherited: the host environment carries credentials that must not enter the sandbox."""
        env: dict[str, str] = {k: os.environ[k] for k in _HOST_BASE_ENV if os.environ.get(k)}
        native_home = to_native_path(self._sandbox_home)
        for name in _REDIRECTED_HOME_VARS:
            env[name] = native_home
        env["PATH"] = os.pathsep.join(self._tool_path_entries(shell))
        env.update({"LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "TERM": "dumb",
                    "HERMES_SANDBOX": "mxc"})
        env.update(self._git_identity_env())
        for key, value in (self.env or {}).items():
            if isinstance(key, str) and value is not None:
                env[key] = str(value)
        with contextlib.suppress(Exception):
            from tools.environments.local import _inject_session_context_env
            _inject_session_context_env(env)
        return env

    def container_request(self, script_path: str, *, settings: Optional[mxc_host.MxcSettings] = None) -> tuple[dict, str, str]:
        """``(config, wxc_exec_path, container_id)`` for running *script_path* under the current policy."""
        settings = settings or self._settings()
        wxc, shell = self._resolve_launcher(settings)
        self._container_seq += 1
        container_id = f"hermes-{self._session_id}-{self._container_seq}"
        cwd = to_native_path(self.cwd)
        # Revalidate at publication, including settings passed as a dataclass and
        # workspace aliases changed since construction. Only scratch/tool roots
        # bypass user-grant protection, through these explicit internal paths.
        readwrite = [*mxc_host.validate_grant_paths([self.workspace_root, *settings.policy.readwrite_paths], writable=True),
                     to_native_path(self.get_temp_dir())]
        readonly = [*self._tool_readonly_grants(shell),
                    *mxc_host.validate_grant_paths(settings.policy.readonly_paths, writable=False)]
        config = build_container_config(
            container_id=container_id,
            command_line=subprocess.list2cmdline([shell, "sh", to_native_path(script_path)]),
            cwd=cwd, env=self.build_sandbox_env(shell),
            readwrite_paths=readwrite, readonly_paths=readonly, network=settings.policy.network)
        return config, wxc, container_id

    # -- process lifecycle -----------------------------------------------------

    def _write_script(self, cmd_string: str) -> str:
        fd, path = tempfile.mkstemp(prefix="command-", suffix=".sh", dir=self.get_temp_dir())
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(cmd_string)
            if not cmd_string.endswith("\n"):
                fh.write("\n")
        self._script_paths.add(path)
        return path

    def _spawn_container(self, cmd_string: str, *, stdin: bool, settings: Optional[mxc_host.MxcSettings] = None) -> subprocess.Popen:
        # Serialize publication with cleanup: a retiring environment must never
        # launch work after its processes have been reaped and scratch removed.
        with self._process_lock:
            if self._closed:
                raise RuntimeError("This MXC environment has been retired; retry with the current terminal policy.")
            proc = self._spawn_container_locked(cmd_string, stdin=stdin, settings=settings)
            self._processes.add(proc)
            return proc

    def _spawn_container_locked(self, cmd_string: str, *, stdin: bool,
                               settings: Optional[mxc_host.MxcSettings]) -> subprocess.Popen:
        settings = settings or self._settings()
        script = self._write_script(cmd_string)
        try:
            config, wxc, container_id = self.container_request(script, settings=settings)
            import base64
            payload = base64.b64encode(json.dumps(config).encode("utf-8")).decode("ascii")
            argv = [wxc, "--config-base64", payload]
            if settings.debug:
                argv.append("--debug")
            proc = subprocess.Popen(
                argv, text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                cwd=to_native_path(self.cwd) if os.path.isdir(to_native_path(self.cwd)) else None,
                creationflags=windows_hide_flags())
        except BaseException:
            self._discard_script(script)
            raise
        proc._hermes_mxc_script = script  # type: ignore[attr-defined]
        proc._hermes_mxc_container = container_id  # type: ignore[attr-defined]
        proc._hermes_mxc_policy = settings.policy  # type: ignore[attr-defined]
        mxc_host.note_container_started()
        return proc

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None) -> ProcessHandle:
        proc = self._spawn_container(cmd_string, stdin=stdin_data is not None)
        if stdin_data is not None:
            _pipe_stdin(proc, stdin_data)
        return proc

    def _wait_for_process(self, proc, timeout: int, **kwargs) -> dict:
        script = getattr(proc, "_hermes_mxc_script", None)
        try:
            result = super()._wait_for_process(proc, timeout, **kwargs)
        finally:
            self._discard_script(script)
            with self._process_lock:
                if proc.poll() is not None:
                    self._processes.discard(proc)
        if script and result.get("output"):
            # The shell prefixes diagnostics with the wrapper script's path; present them as
            # ordinary shell errors instead of leaking the per-command temp file name.
            result["output"] = re.sub(re.escape(to_forward_slashes(script)) + r": (?:eval: )?(?:line \d+: )?",
                                      "sh: ", result["output"])
        result["sandbox"] = {"backend": "mxc", "container": getattr(proc, "_hermes_mxc_container", None)}
        self._last_policy = getattr(proc, "_hermes_mxc_policy", None)
        return result

    def _discard_script(self, path: Optional[str]) -> None:
        if not path:
            return
        self._script_paths.discard(path)
        with contextlib.suppress(OSError):
            os.unlink(path)

    def _kill_process(self, proc: ProcessHandle):
        # The launcher's job object takes the sandboxed tree down with it.
        with contextlib.suppress(Exception):
            proc.kill()

    def spawn_background_process(self, command: str, cwd: str | None = None) -> subprocess.Popen:
        """A long-lived container for ``terminal(background=true)``: the launcher stays alive for
        as long as the command runs, so servers and watchers survive across later tool calls."""
        settings = self._settings()
        script = posix_wrap_command_script(
            command, passthrough_names=self._snapshot_excluded_passthrough_names(),
            snapshot_ready=self._snapshot_ready, **self._snapshot_script_kwargs(cwd or self.cwd))
        return self._spawn_container(script, stdin=False, settings=settings)

    # -- session bootstrap and wrapping --------------------------------------

    def init_session(self):
        bootstrap = posix_bootstrap_script(
            excluded_names=self._snapshot_excluded_passthrough_names(), **self._snapshot_script_kwargs(self.cwd))
        try:
            proc = self._run_bash(bootstrap, timeout=self._snapshot_timeout)
            result = self._wait_for_process(proc, timeout=self._snapshot_timeout)
            if int(result.get("returncode") or 0) != 0:
                raise RuntimeError(f"snapshot bootstrap failed with exit code {result.get('returncode')}: "
                                   f"{(result.get('output') or '').strip()[:400]}")
            self._snapshot_ready = True
            self._update_cwd(result)
            logger.info("MXC session snapshot created (session=%s, cwd=%s)", self._session_id, self.cwd)
        except Exception as exc:
            self._snapshot_ready = False
            logger.warning("MXC init_session failed (session=%s): %s", self._session_id, exc)

    def _wrap_command(self, command: str, cwd: str) -> str:
        return posix_wrap_command_script(
            command, passthrough_names=self._snapshot_excluded_passthrough_names(),
            snapshot_ready=self._snapshot_ready, **self._snapshot_script_kwargs(cwd))

    def execute(self, command: str, cwd: str = "", **kwargs) -> dict:
        result = super().execute(command, cwd, **kwargs)
        self._annotate_denials(result)
        return result

    def _annotate_denials(self, result: dict) -> None:
        output = result.get("output") or ""
        denied = find_denials(output) if output else []
        if not denied:
            return
        policy = getattr(self, "_last_policy", None) or self._settings().policy
        sandbox = result.setdefault("sandbox", {"backend": "mxc"})
        sandbox["denied"] = denied
        sandbox["policy"] = policy.as_dict()
        result["output"] = output.rstrip("\n") + "\n\n" + denial_note(denied, workspace=self.workspace_root, policy=policy)

    # -- cleanup ----------------------------------------------------------------

    def cleanup(self):
        with self._process_lock:
            self._closed = True
            for proc in self._processes:
                if proc.poll() is None:
                    proc.kill()
                # Job-object teardown precedes removal of its executable state.
                # Propagate failure so policy reconciliation cannot claim success.
                proc.wait(timeout=10)
            self._processes.clear()
            scratch = getattr(self, "_scratch_dir", None)
            if scratch and os.path.exists(scratch):
                shutil.rmtree(scratch)
            self._script_paths.clear()
