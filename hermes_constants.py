"""Shared constants for Hermes Agent.

Import-safe module with no dependencies — can be imported from anywhere
without risk of circular imports.
"""

import os
import shutil
import stat
import sys
from contextvars import ContextVar, Token
from pathlib import Path


_profile_fallback_warned: bool = False
_UNSET = object()
_HERMES_HOME_OVERRIDE: ContextVar[str | object] = ContextVar(
    "_HERMES_HOME_OVERRIDE", default=_UNSET
)

# ── TUI busy-indicator styles ─────────────────────────────────────────
# Single source of truth shared by the CLI /indicator command, the TUI
# gateway config handler, and the /help command registry. Keep in sync
# with ``INDICATOR_STYLES`` / ``DEFAULT_INDICATOR_STYLE`` in
# ``ui-tui/src/app/interfaces.ts`` on the frontend side.
INDICATOR_STYLES: tuple[str, ...] = ("ascii", "emoji", "kaomoji", "unicode")
DEFAULT_INDICATOR_STYLE: str = "kaomoji"


def set_hermes_home_override(path: str | Path | None) -> Token:
    """Set a context-local Hermes home override and return its reset token.

    This is for in-process, per-task scoping.  It deliberately does not mutate
    ``os.environ`` because that is shared by every thread in the process.
    """
    value: str | object = _UNSET if path is None else str(path)
    return _HERMES_HOME_OVERRIDE.set(value)


def reset_hermes_home_override(token: Token) -> None:
    """Restore the previous context-local Hermes home override."""
    _HERMES_HOME_OVERRIDE.reset(token)


def get_hermes_home_override() -> str | None:
    """Return the active context-local Hermes home override, if any."""
    override = _HERMES_HOME_OVERRIDE.get()
    if override is _UNSET or not override:
        return None
    return str(override)


def _get_platform_default_hermes_home() -> Path:
    """Return the platform-native default Hermes home path."""
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def _hermes_home_from_env() -> Path:
    """Resolve HERMES_HOME from the process environment only.

    Reads the ``HERMES_HOME`` env var, falling back to the platform-native
    default.  Deliberately ignores the context-local override installed by
    :func:`set_hermes_home_override`, so this reflects the process/launch
    scope rather than a per-task profile.  Shared by :func:`get_hermes_home`
    and :func:`get_process_hermes_home` so the two never drift.
    """
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    return _get_platform_default_hermes_home()


def _warn_profile_fallback_once() -> None:
    """Warn once when falling back to the default home while a profile is active.

    Guard: if a non-default profile is sticky-active but ``HERMES_HOME`` is
    unset, the fallback to the default profile is almost certainly wrong.
    """
    global _profile_fallback_warned
    if _profile_fallback_warned:
        return
    try:
        fallback_home = _get_platform_default_hermes_home()
        active_path = fallback_home / "active_profile"
        active = active_path.read_text(encoding="utf-8").strip() if active_path.exists() else ""
    except (UnicodeDecodeError, OSError):
        active = ""
    if active and active != "default":
        _profile_fallback_warned = True
        # Write directly to stderr.  We intentionally do NOT route this
        # through ``logging`` because (a) this function is called at
        # module-import time from 30+ sites, often before logging is
        # configured, and (b) root-logger propagation would double-emit
        # on consoles where a StreamHandler is already attached.
        msg = (
            f"[HERMES_HOME fallback] HERMES_HOME is unset but active "
            f"profile is {active!r}. Falling back to {fallback_home}, which "
            f"is the DEFAULT profile — not {active!r}. Any data this "
            f"process writes will land in the wrong profile. The "
            f"subprocess spawner should pass HERMES_HOME explicitly "
            f"(see issue #18594)."
        )
        try:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()
        except Exception:
            pass


def get_hermes_home() -> Path:
    """Return the Hermes home directory (default: platform-native path).

    Resolution order: context-local override (see
    :func:`set_hermes_home_override`) → ``HERMES_HOME`` env var → the
    platform-native default.  This is the single source of truth — all other
    copies should import this.

    When ``HERMES_HOME`` is unset but an ``active_profile`` file indicates
    a non-default profile is active, logs a loud one-shot warning to
    ``errors.log`` so cross-profile data corruption is diagnosable instead
    of silent.  Behavior is unchanged otherwise — we still return
    the platform-native default — because raising here would brick 30+ module-level
    callers that import this at load time.  Subprocess spawners are
    expected to propagate ``HERMES_HOME`` explicitly (see the systemd
    template in ``hermes_cli/gateway.py`` and the kanban dispatcher in
    ``hermes_cli/kanban_db.py``).  See https://github.com/NousResearch/hermes-agent/issues/18594.
    """
    override = get_hermes_home_override()
    if override:
        return Path(override)

    if not os.environ.get("HERMES_HOME", "").strip():
        _warn_profile_fallback_once()

    return _hermes_home_from_env()


def hermes_home_key(path: str | Path | None = None) -> str:
    """Return a stable key for a Hermes home/profile directory.

    Runtime registries use this key to isolate plugin-owned entries while
    keeping built-in registrations process-global.  ``strict=False`` preserves
    useful behavior for profiles whose directories have not been created yet.
    """
    candidate = Path(path) if path is not None else get_hermes_home()
    resolved = candidate.expanduser().resolve(strict=False)
    return os.path.normcase(str(resolved))


def get_process_hermes_home() -> Path:
    """Return the Hermes home for the running process, ignoring task overrides.

    Unlike :func:`get_hermes_home`, this never follows the context-local
    override set by :func:`set_hermes_home_override`.  It resolves only the
    process ``HERMES_HOME`` env var (falling back to the platform default),
    so it reflects the scope the process was launched under **as long as
    nothing mutates ``os.environ`` in-process**.

    Use this for machine/process-level dashboard-owned assets — theme YAML,
    dashboard plugin manifests — that live under the server's launch home and
    must stay visible even while a request is scoped to another profile (e.g.
    the embedded ``/chat`` running under ``--open-profile``).  Do NOT use it
    for genuinely profile-scoped data (memories, backups, checkpoints,
    provider config) — those should keep following the override.
    """
    return _hermes_home_from_env()


# Process-level memo for get_default_hermes_root(). The function resolves
# HERMES_HOME against the native home on every call (~80us of path
# resolution), and it is called at 31+ sites — every _load_global_auth_store()
# (per provider row in the /model picker), kanban, backup, gateway, update.
# Its result depends only on (HERMES_HOME, platform native home), which are
# compared for free on each call, so the memo is freshness-correct even if a
# test or plugin mutates HERMES_HOME mid-process.
_default_hermes_root_memo: "tuple[str, str, Path] | None" = None


def get_default_hermes_root() -> Path:
    """Return the root Hermes directory for profile-level operations.

    In standard deployments this is the platform-native Hermes home
    (``~/.hermes`` on POSIX, ``%LOCALAPPDATA%\\hermes`` on native Windows).

    In Docker or custom deployments where ``HERMES_HOME`` points outside
    ``~/.hermes`` (e.g. ``/opt/data``), returns ``HERMES_HOME`` directly
    — that IS the root.

    In profile mode where ``HERMES_HOME`` is ``<root>/profiles/<name>``,
    returns ``<root>`` so that ``profile list`` can see all profiles.
    Works both for standard (``~/.hermes/profiles/coder``) and Docker
    (``/opt/data/profiles/coder``) layouts.

    Import-safe — no dependencies beyond stdlib.
    """
    global _default_hermes_root_memo
    native_home = _get_platform_default_hermes_home()
    env_home = os.environ.get("HERMES_HOME", "")
    if _default_hermes_root_memo is not None:
        memo_native, memo_env, memo_result = _default_hermes_root_memo
        if memo_native == str(native_home) and memo_env == env_home:
            return memo_result

    if not env_home:
        result = native_home
    else:
        env_path = Path(env_home)
        try:
            env_path.resolve().relative_to(native_home.resolve())
            # HERMES_HOME is under ~/.hermes (normal or profile mode)
            result = native_home
        except ValueError:
            # Docker / custom deployment.
            # Check if this is a profile path: <root>/profiles/<name>
            # If the immediate parent dir is named "profiles", the root is
            # the grandparent — this covers Docker profiles correctly.
            if env_path.parent.name == "profiles":
                result = env_path.parent.parent
            else:
                # Not a profile path — HERMES_HOME itself is the root
                result = env_path
    _default_hermes_root_memo = (str(native_home), env_home, result)
    return result


# ─── Install-scoped runtime dir (design: 2026-08-12_hermes-home-lifetime-split) ──
#
# HERMES_HOME holds PROFILE state (config, sessions, skills). Install-scoped
# artifacts — managed binaries, venvs, true caches, update bookkeeping —
# belong to ONE install of Hermes and live beside its code in
# ``<install root>/.hermes-runtime/``. Two installs sharing one home must
# never share these (different node versions, venv ABIs, update stamps).

# Install paths live in `installation.paths` — the bottom of that layer,
# where the pin/fact tables and the provisioner can reach them without
# importing back up into this module. Re-exported here because ~40 callers
# already ask hermes_constants for them.
from installation.paths import (  # noqa: E402
    RUNTIME_DIR_NAME,
    get_install_root,
    get_runtime_dir,
    reset_install_root_override,
    set_install_root_override,
)


def get_optional_skills_dir(default: Path | None = None) -> Path:
    """Return the optional-skills directory, honoring package-manager wrappers.

    Packaged installs may ship ``optional-skills`` outside the Python package
    tree and expose it via ``HERMES_OPTIONAL_SKILLS``.
    """
    override = os.getenv("HERMES_OPTIONAL_SKILLS", "").strip()
    if override:
        return Path(override)
    if default is not None:
        return default
    return get_hermes_home() / "optional-skills"


def get_optional_mcps_dir(default: Path | None = None) -> Path:
    """Return the optional-mcps directory, honoring package-manager wrappers.

    Mirrors :func:`get_optional_skills_dir` for the MCP catalog (Nous-approved
    Model Context Protocol servers shipped with the repo but disabled by
    default). Packaged installs may ship ``optional-mcps`` outside the Python
    package tree and expose it via ``HERMES_OPTIONAL_MCPS``.
    """
    override = os.getenv("HERMES_OPTIONAL_MCPS", "").strip()
    if override:
        return Path(override)
    if default is not None:
        return default
    return get_hermes_home() / "optional-mcps"


def get_bundled_skills_dir(default: Path | None = None) -> Path:
    """Return the bundled skills directory for source and packaged installs.

    Resolution order:
        1. ``HERMES_BUNDLED_SKILLS`` env var (Nix wrapper / explicit override)
        2. Caller-supplied ``default`` (typically the source-checkout path)
        3. ``<HERMES_HOME>/skills`` last-resort
    """
    override = os.getenv("HERMES_BUNDLED_SKILLS", "").strip()
    if override:
        return Path(override)
    if default is not None:
        return default
    return get_hermes_home() / "skills"


def get_hermes_dir(
    new_subpath: str,
    old_name: str,
    *,
    home: Path | None = None,
) -> Path:
    """Resolve a Hermes subdirectory with backward compatibility.

    New installs get the consolidated layout (e.g. ``cache/images``).
    Existing installs that already have the old path (e.g. ``image_cache``)
    keep using it — no migration required.

    A bare empty ``<old_name>/`` directory does **not** count as "the
    legacy install is in use" — install scaffolds, manual ``mkdir`` work,
    and cleared-then-abandoned locations all create empty stubs that
    would otherwise silently shadow real data populated at
    ``<new_subpath>/``. See #27602 for the pairing-store regression where
    a dormant empty ``pairing/`` orphaned approved-user data in
    ``platforms/pairing/``.

    Args:
        new_subpath: Preferred path relative to HERMES_HOME (e.g. ``"cache/images"``).
        old_name: Legacy path relative to HERMES_HOME (e.g. ``"image_cache"``).
        home: Optional explicit Hermes home. Profile-aware callers that manage
            more than one home in the same process use this instead of
            temporarily mutating the process or context-local HERMES_HOME.

    Returns:
        Absolute ``Path`` — legacy location if it exists with content,
        otherwise the new location.
    """
    home = home or get_hermes_home()
    old_path = home / old_name
    if _legacy_path_has_content(old_path):
        return old_path
    return home / new_subpath


def agent_browser_runnable(path: str | None) -> bool:
    """Return True only when *path* is an agent-browser CLI that actually runs.

    A bare presence check (``shutil.which`` / ``Path.exists``) is not enough:
    agent-browser's npm ``postinstall`` re-points a *global* install symlink
    (e.g. ``/opt/homebrew/bin/agent-browser``) at our local
    ``node_modules/agent-browser/bin/...`` binary, which then disappears on the
    next ``hermes update`` — leaving a **dangling symlink** that ``which`` still
    reports but exec fails on with exit 127 (issue #48521). Callers that trust
    such a path silently break every browser tool.

    This validates the candidate by resolving it to a real, executable file and
    running ``--version`` with a short timeout. Returns True only on a clean
    (exit 0) run, so a dead/wrong-arch/hung binary is rejected and the caller
    can fall through to the next resolution candidate.

    Special cases:
      * ``None`` / empty → False.
      * The ``"npx agent-browser"`` fallback form (contains a space, not a real
        file) → True; npx resolves and validates the package at run time, so
        there is nothing to stat here.
    """
    if not path:
        return False
    # The npx fallback is a two-token command string, not a filesystem path.
    if " " in path and path.split()[0].endswith("npx"):
        return True
    # exists() follows symlinks — a dangling link returns False here, so we
    # never even spawn a subprocess for the broken-link case.
    if not os.path.exists(path) or not os.access(path, os.X_OK):
        return False
    import subprocess

    try:
        from hermes_cli._subprocess_compat import windows_hide_flags
        from installation import env as runtime_env

        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            timeout=10,
            env=runtime_env.with_managed_runtimes(),
            creationflags=windows_hide_flags(),
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    return result.returncode == 0


def _legacy_path_has_content(path: Path) -> bool:
    """Return ``True`` iff ``path`` exists and has content worth honouring.

    A populated *directory* (any entry inside) counts. A non-directory
    file at ``path`` also counts — the consumer presumably wrote it.
    An empty directory does **not** count, so a stale empty
    legacy stub falls through to the new layout. If the path cannot be
    inspected (``PermissionError`` on ``stat``/``iterdir``, or any other
    ``OSError`` short of "not found"), assume occupied so we don't
    accidentally orphan legacy data. Only a genuine
    ``FileNotFoundError`` counts as absent.

    Symlinks are resolved before judging content: a symlink pointing at a
    populated directory (or any existing non-directory target) counts, but
    a **dangling** symlink (broken target) does **not** — it must not be
    allowed to shadow populated new-layout data, matching the old
    ``exists()`` gate's behaviour for broken links.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        # PermissionError on a parent, or any other inspection failure:
        # treat as occupied rather than silently orphaning legacy data.
        return True
    if stat.S_ISLNK(st.st_mode):
        # Resolve the link's target. A dangling symlink has no content and
        # must not shadow the new layout; a valid one is judged on its target.
        try:
            target_st = path.stat()  # follows the link
        except FileNotFoundError:
            return False  # dangling symlink → fall through to new layout
        except OSError:
            return True  # can't resolve → assume occupied, don't orphan data
        if not stat.S_ISDIR(target_st.st_mode):
            return True
        # target is a directory — fall through to the iterdir() emptiness check
    elif not stat.S_ISDIR(st.st_mode):
        return True
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    except OSError:
        return True
    return True


def display_hermes_home() -> str:
    """Return a user-friendly display string for the current HERMES_HOME.

    Uses ``~/`` shorthand for readability::

        default:  ``~/.hermes``
        profile:  ``~/.hermes/profiles/coder``
        custom:   ``/opt/hermes-custom``

    Use this in **user-facing** print/log messages instead of hardcoding
    ``~/.hermes``.  For code that needs a real ``Path``, use
    :func:`get_hermes_home` instead.
    """
    home = get_hermes_home()
    try:
        return "~/" + str(home.relative_to(Path.home()))
    except ValueError:
        return str(home)


def secure_parent_dir(path: Path) -> None:
    """Chmod ``0o700`` on the parent directory of *path*, but only if safe.

    Refuses to chmod ``/`` or any top-level directory (resolved parent with
    fewer than 3 parts, i.e. ``/`` or any direct child like ``/usr``) to
    prevent catastrophic host bricking when ``HERMES_HOME`` or other path
    env vars resolve to an unexpected location.

    See https://github.com/NousResearch/hermes-agent/issues/25821.
    """
    parent = path.parent.resolve()
    # Refuse root and its direct children (/usr, /home, /var, /tmp, …).
    if parent == Path("/") or len(parent.parts) < 3:
        return
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass


def _norm_home_path(path: str | None) -> str:
    """Return a comparable absolute path string, or ``""`` for empty input."""
    raw = (path or "").strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(raw)))
    except Exception:
        return os.path.normcase(raw)


def _profile_home_path(env: dict[str, str] | None = None) -> str | None:
    """Return ``{HERMES_HOME}/home`` when the profile-home directory exists."""
    hermes_home = get_hermes_home_override() or (env or {}).get("HERMES_HOME") or os.getenv("HERMES_HOME")
    if not hermes_home:
        return None
    profile_home = os.path.join(hermes_home, "home")
    if os.path.isdir(profile_home):
        return profile_home
    return None


def _is_profile_home(candidate: str | None, profile_home: str | None) -> bool:
    return bool(candidate and profile_home and _norm_home_path(candidate) == _norm_home_path(profile_home))


def _iter_real_home_candidates(env: dict[str, str] | None = None) -> list[str]:
    """Return likely OS-user home candidates in trust order."""
    env = env or {}
    candidates: list[str] = []
    explicit = str(env.get("HERMES_REAL_HOME") or os.getenv("HERMES_REAL_HOME", "")).strip()
    if explicit:
        candidates.append(explicit)
    home = str(env.get("HOME") or os.getenv("HOME", "")).strip()
    if home:
        candidates.append(home)
    try:
        import pwd

        pw_home = pwd.getpwuid(os.getuid()).pw_dir.strip()  # windows-footgun: ok — POSIX-only module inside try/except
        if pw_home:
            candidates.append(pw_home)
    except Exception:
        pass
    userprofile = str(env.get("USERPROFILE") or os.getenv("USERPROFILE", "")).strip()
    if userprofile:
        candidates.append(userprofile)
    drive = str(env.get("HOMEDRIVE") or os.getenv("HOMEDRIVE", "")).strip()
    path = str(env.get("HOMEPATH") or os.getenv("HOMEPATH", "")).strip()
    if drive and path:
        candidates.append(f"{drive}{path}" if path.startswith(("\\", "/")) else os.path.join(drive, path))
    expanded = os.path.expanduser("~")
    if expanded and expanded != "~":
        candidates.append(expanded)
    return candidates


def get_real_home(env: dict[str, str] | None = None) -> str:
    """Return the OS user's real home directory, avoiding Hermes profile HOME.

    ``HERMES_HOME`` scopes Hermes state. ``HOME`` is reserved for the OS/user
    account and the many external CLIs that store credentials under ``~``.
    If a parent process is already running with ``HOME={HERMES_HOME}/home``,
    this helper repairs back to the account home when possible.
    """
    profile_home = _profile_home_path(env)
    seen: set[str] = set()
    for candidate in _iter_real_home_candidates(env):
        key = _norm_home_path(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        if not _is_profile_home(candidate, profile_home):
            return candidate
    return "/tmp"


def get_subprocess_home(env: dict[str, str] | None = None) -> str | None:
    """Return a subprocess ``HOME`` override, if one should be applied.

    Policy is controlled by ``terminal.home_mode`` (bridged to
    ``TERMINAL_HOME_MODE``):

    * ``auto`` (default): host installs keep the real user HOME; containers use
      ``{HERMES_HOME}/home`` for persistent state. If a host parent already has
      HOME pointed at the profile home, repair subprocesses back to real HOME.
    * ``real``: always prefer the real OS-user HOME.
    * ``profile``: use ``{HERMES_HOME}/home`` when it exists, preserving the
      older strict per-profile tool-config isolation.
    """
    env = env or {}
    profile_home = _profile_home_path(env)
    mode = str(env.get("TERMINAL_HOME_MODE") or os.getenv("TERMINAL_HOME_MODE", "auto")).strip().lower() or "auto"
    if mode in {"isolated", "profile_home", "profile-home"}:
        mode = "profile"
    if mode in {"host", "user", "real_home", "real-home"}:
        mode = "real"

    if mode == "profile":
        return profile_home

    real_home = get_real_home(env)
    current_home = str(env.get("HOME") or os.getenv("HOME", "")).strip()
    if mode == "real":
        return real_home if _norm_home_path(real_home) != _norm_home_path(current_home) else None

    if profile_home and is_container():
        return profile_home
    if _is_profile_home(current_home, profile_home):
        return real_home if _norm_home_path(real_home) != _norm_home_path(current_home) else None
    return None


def apply_subprocess_home_env(env: dict[str, str]) -> None:
    """Apply Hermes' subprocess HOME contract to *env* in-place."""
    real_home = get_real_home(env)
    if real_home:
        env["HERMES_REAL_HOME"] = real_home
    home = get_subprocess_home(env)
    if home:
        env["HOME"] = home


VALID_REASONING_EFFORTS = (
    "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
)


def parse_reasoning_effort(effort) -> dict | None:
    """Parse a reasoning effort level into a config dict.

    Valid levels: "none", "minimal", "low", "medium", "high", "xhigh", "max",
    "ultra".
    Returns None when the input is empty or unrecognized (caller uses default).
    Returns {"enabled": False} for "none" (aliases: "false", "disabled", and
    YAML boolean False — users write ``reasoning_effort: false``/``off``/``no``
    in config.yaml and YAML hands us a bool, which must mean disabled, not
    "fall back to the default and keep thinking").
    Returns {"enabled": True, "effort": <level>} for valid effort levels.
    """
    if effort is False:
        return {"enabled": False}
    if effort is None or effort is True:
        return None
    effort = str(effort)
    if not effort.strip():
        return None
    effort = effort.strip().lower()
    if effort in {"none", "false", "disabled"}:
        return {"enabled": False}
    if effort in VALID_REASONING_EFFORTS:
        return {"enabled": True, "effort": effort}
    return None


def _canonical_model_variants(model: str) -> list[str]:
    """Generate bounded spelling variants for tolerant override matching.

    Model names mix two types of separators:
    - **Word separators**: dashes between words (``claude-opus``)
    - **Version separators**: dots or dashes between version digits (``4.5``, ``4-5``)

    The tricky case is that ``.`` appears in BOTH roles (word sep in some
    spellings, version sep in others), so a blanket ``.replace('.', '-')``
    is lossy — it collapses version dots into dashes and no later step
    recovers the canonical form (``claude-opus-4.5``).

    Strategy: generate a small set of base forms, then apply version-dot
    recovery to EACH of them. This ensures symmetry:
    ``claude-opus-4.5``, ``claude-opus-4-5``, and ``claude-opus.4.5`` all
    produce the same variant set.

    Steps:
    1. Exact input
    2. Dots/dashes cross-substitution on the entire string
    3. Version-dot recovery applied to ALL derivatives
    4. Strip provider/aggregator prefix → bare model variants
    5. Apply version-dot recovery to bare derivatives
    6. Prepend known provider/aggregator prefixes

    Duplicates removed in insertion order (exact always wins).
    """
    import re

    # Version-dot regexes — digit-separator-digit interconversion
    _dash_to_dot = lambda s: re.sub(r'(\d)-(\d)', r'\1.\2', s)
    _dot_to_dash = lambda s: re.sub(r'(\d)\.(\d)', r'\1-\2', s)

    seen = set()
    variants = []

    def _add(v):
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    def _add_with_derivatives(s):
        """Add s plus its dots↔dashes and version-dot derivatives."""
        _add(s)
        all_dashed = s.replace('.', '-')
        _add(all_dashed)
        all_dotted = s.replace('-', '.')
        _add(all_dotted)
        # Version-dot recovery on each base form
        _add(_dash_to_dot(s))
        _add(_dot_to_dash(s))
        _add(_dash_to_dot(all_dashed))
        _add(_dot_to_dash(all_dotted))

    # 1-3. Base variants for the full string
    _add_with_derivatives(model)

    # Split by / to handle provider prefix
    parts = model.split('/')

    # 4. Bare model variants (strip provider/aggregator prefix)
    if len(parts) >= 2:
        bare = parts[-1]
        _add_with_derivatives(bare)

    # Strip aggregator only (3+ parts)
    # e.g. "openrouter/anthropic/claude-opus-4.5" → "anthropic/claude-opus-4.5"
    if len(parts) >= 3:
        _add_with_derivatives('/'.join(parts[1:]))

    # 5. Prepend known provider prefixes to bare variants
    known_providers = (
        'anthropic', 'openai', 'google', 'openrouter', 'groq', 'mistral',
        'xai', 'cohere', 'perplexity', 'together', 'fireworks', 'deepseek',
    )
    bare_variants = [v for v in variants if '/' not in v]
    for v in bare_variants:
        for provider in known_providers:
            _add(f"{provider}/{v}")

    # Prepend aggregator to single-slash variants
    single_slash_variants = [v for v in variants if v.count('/') == 1]
    known_aggregators = ('openrouter', 'opencode', 'fireworks', 'groq', 'together')
    for v in single_slash_variants:
        for agg in known_aggregators:
            _add(f"{agg}/{v}")

    return variants


def resolve_per_model_reasoning_effort(model: str, overrides: dict | None) -> dict | None:
    """Lookup a per-model reasoning_effort override with spelling-tolerance.

    Args:
        model: The model string (any spelling — exact, normalized, bare,
               with provider prefix, etc.)
        overrides: The dict of per-model overrides from
                   agent.reasoning_overrides in config.yaml. Keys can be
                   any sensible spelling of the model name.

    Returns:
        The parsed reasoning_config dict if a match is found,
        None otherwise (caller should fall back to global reasoning_effort).

    Resolution order:
    1. Exact match
    2. Dots ↔ dashes variants
    3. Strip provider prefix (bare model name only)
    4. Strip aggregator prefix (middle segment only)
    5. Prepend known aggregator prefixes to bare/single-slash variants

    First non-None parse_reasoning_effort result wins.
    """
    if not overrides or not isinstance(overrides, dict) or not model:
        return None

    for variant in _canonical_model_variants(model):
        if variant in overrides:
            result = parse_reasoning_effort(overrides[variant])
            if result is not None:
                return result

    return None


def resolve_reasoning_config(cfg: dict | None, model: str = "") -> dict | None:
    """Resolve the effective reasoning config for *model* from a config dict.

    Single chokepoint for reasoning-effort resolution, shared by every
    surface (CLI startup, messaging gateway, Desktop/TUI, cron, ``/model``
    switch, fallback activation). Priority:

    1. Per-model override from ``agent.reasoning_overrides``
       (spelling-tolerant — see :func:`resolve_per_model_reasoning_effort`)
    2. Global ``agent.reasoning_effort`` — the raw value is passed through
       so a YAML boolean ``False`` (``reasoning_effort: false``/``off``/
       ``no``) means "thinking disabled", never silently re-enabled.

    Session-scoped overrides (gateway ``/reasoning --session``) are resolved
    by the caller BEFORE this function — they always win.

    Args:
        cfg: A loaded config dict (any of the three loaders' shapes — only
             the ``agent`` and ``model`` sections are read).
        model: The effective model for this surface/session. When empty,
               it is derived from the config's ``model`` section (string
               form, or a dict's ``default``/``model`` keys).

    Returns:
        The parsed reasoning config dict, or None when unset/unrecognized
        (caller uses the provider default).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    agent_cfg = cfg.get("agent")
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}

    if not model:
        model_cfg = cfg.get("model")
        if isinstance(model_cfg, str):
            model = model_cfg.strip()
        elif isinstance(model_cfg, dict):
            model = str(
                model_cfg.get("default") or model_cfg.get("model") or ""
            ).strip()
        else:
            model = ""

    overrides = agent_cfg.get("reasoning_overrides") or {}
    per_model = resolve_per_model_reasoning_effort(model, overrides)
    if per_model is not None:
        return per_model

    # Global fallback — keep the raw value; coercing with ``or ""`` turns a
    # YAML boolean False into "", silently re-enabling thinking for users
    # who explicitly disabled it.
    effort = agent_cfg.get("reasoning_effort", "")
    result = parse_reasoning_effort(effort)
    if effort and str(effort).strip() and result is None:
        import logging
        logging.getLogger(__name__).warning(
            "Unknown reasoning_effort '%s', using default (medium)", effort
        )
    return result


_wsl_detected: bool | None = None


def is_wsl() -> bool:
    """Return True when running inside WSL (Windows Subsystem for Linux).

    Checks ``/proc/version`` for the ``microsoft`` marker that both WSL1
    and WSL2 inject.  Result is cached for the process lifetime.
    Import-safe — no heavy deps.
    """
    global _wsl_detected
    if _wsl_detected is not None:
        return _wsl_detected
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            _wsl_detected = "microsoft" in f.read().lower()
    except Exception:
        _wsl_detected = False
    return _wsl_detected


def windows_path_to_wsl(path: str) -> str | None:
    """Convert a Windows drive path (``C:\\...``) to its ``/mnt/<drive>/...`` form."""
    import re

    match = re.match(r"^([A-Za-z]):[\\/](.*)$", str(path or "").strip())
    if not match:
        return None
    drive = match.group(1).lower()
    tail = match.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{tail}"


def wsl_unc_path_to_posix(path: str) -> str | None:
    """Convert a Windows WSL UNC path (``\\\\wsl.localhost\\<distro>\\...`` or the
    legacy ``\\\\wsl$\\...``) to a POSIX path inside the distro."""
    import re

    normalized = str(path or "").strip().replace("/", "\\")
    match = re.match(r"^\\\\wsl(?:\.localhost|\$)\\[^\\]+\\(.*)$", normalized, re.IGNORECASE)
    if not match:
        return None
    tail = match.group(1).replace("\\", "/")
    return f"/{tail}" if tail else "/"


def translate_cwd_for_wsl_backend(cwd: str) -> str:
    """Normalize a cross-boundary cwd when Hermes itself runs inside WSL.

    A Windows-host UI (native picker / drive path / ``\\\\wsl.localhost\\`` UNC)
    can hand the WSL backend a path it can't ``chdir`` into. Map it to the POSIX
    equivalent so the picker, sidebar, and sessions all agree on the workspace.
    No-op off WSL and for paths that are already POSIX.
    """
    if not is_wsl():
        return cwd
    for translator in (wsl_unc_path_to_posix, windows_path_to_wsl):
        translated = translator(cwd)
        if translated is not None:
            return translated
    return cwd


_container_detected: bool | None = None


def is_container() -> bool:
    """Return True when running inside a container.

    Recognizes Docker (``/.dockerenv``), Podman (``/run/.containerenv``),
    and — via ``/proc/1/cgroup`` — the docker/podman/lxc cgroup-v1 markers.

    cgroup v2 collapses ``/proc/1/cgroup`` to a single ``0::/`` line with no
    runtime marker, so containerd/CRI-O runtimes (the common case on
    Kubernetes/k3s) were previously missed. To cover those, also check:
      * ``KUBERNETES_SERVICE_HOST`` env var — set in every Kubernetes pod.
      * ``kubepods`` / ``containerd`` / ``crio`` markers in ``/proc/1/cgroup``.
      * the same markers in ``/proc/self/mountinfo`` (cgroup-v2 fallback).

    Result is cached for the process lifetime.  Import-safe — no heavy deps.

    See: NousResearch/hermes-agent#47111
    """
    global _container_detected
    if _container_detected is not None:
        return _container_detected
    if os.path.exists("/.dockerenv"):
        _container_detected = True
        return True
    if os.path.exists("/run/.containerenv"):
        _container_detected = True
        return True
    # Kubernetes always injects this into pod containers; absent on hosts.
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        _container_detected = True
        return True
    _CGROUP_MARKERS = ("docker", "podman", "/lxc/", "kubepods", "containerd", "crio")
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as f:
            cgroup = f.read()
            if any(marker in cgroup for marker in _CGROUP_MARKERS):
                _container_detected = True
                return True
    except OSError:
        pass
    # cgroup v2: /proc/1/cgroup is just "0::/" with no marker. The container
    # runtime still shows up in the mount table (overlay rootfs, runtime mount
    # paths), so scan mountinfo as a last resort.
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as f:
            mountinfo = f.read()
            if any(marker in mountinfo for marker in ("kubepods", "containerd", "crio")):
                _container_detected = True
                return True
    except OSError:
        pass
    _container_detected = False
    return False


# ─── Well-Known Paths ─────────────────────────────────────────────────────────


def get_config_path() -> Path:
    """Return the path to ``config.yaml`` under HERMES_HOME.

    Replaces the ``get_hermes_home() / "config.yaml"`` pattern repeated
    in 7+ files (skill_utils.py, hermes_logging.py, hermes_time.py, etc.).
    """
    return get_hermes_home() / "config.yaml"


def get_skills_dir() -> Path:
    """Return the path to the skills directory under HERMES_HOME."""
    return get_hermes_home() / "skills"



def get_env_path() -> Path:
    """Return the path to the ``.env`` file under HERMES_HOME."""
    return get_hermes_home() / ".env"


# ─── Network Preferences ─────────────────────────────────────────────────────


def apply_ipv4_preference(force: bool = False) -> None:
    """Monkey-patch ``socket.getaddrinfo`` to prefer IPv4 connections.

    On servers with broken or unreachable IPv6, Python tries AAAA records
    first and hangs for the full TCP timeout before falling back to IPv4.
    This affects httpx, requests, urllib, the OpenAI SDK — everything that
    uses ``socket.getaddrinfo``.

    When *force* is True, patches ``getaddrinfo`` so that calls with
    ``family=AF_UNSPEC`` (the default) resolve as ``AF_INET`` instead,
    skipping IPv6 entirely.  If no A record exists, falls back to the
    original unfiltered resolution so pure-IPv6 hosts still work.

    Safe to call multiple times — only patches once.
    Set ``network.force_ipv4: true`` in ``config.yaml`` to enable.
    """
    if not force:
        return

    import socket

    # Guard against double-patching
    if getattr(socket.getaddrinfo, "_hermes_ipv4_patched", False):
        return

    _original_getaddrinfo = socket.getaddrinfo

    def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if family == 0:  # AF_UNSPEC — caller didn't request a specific family
            try:
                return _original_getaddrinfo(
                    host, port, socket.AF_INET, type, proto, flags
                )
            except socket.gaierror:
                # No A record — fall back to full resolution (pure-IPv6 hosts)
                return _original_getaddrinfo(host, port, family, type, proto, flags)
        return _original_getaddrinfo(host, port, family, type, proto, flags)

    _ipv4_getaddrinfo._hermes_ipv4_patched = True  # type: ignore[attr-defined]
    socket.getaddrinfo = _ipv4_getaddrinfo  # type: ignore[assignment]


# ─── Streaming Response Constants ────────────────────────────────────────────

# Response ID for partial stream stubs used during error recovery
PARTIAL_STREAM_STUB_ID = "partial-stream-stub"

FINISH_REASON_LENGTH = "length"


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = f"{OPENROUTER_BASE_URL}/models"

AI_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"


# ─── Venv layout ─────────────────────────────────────────────────────────────

def venv_bin_dir(venv_dir, *, windows: bool | None = None) -> Path:
    """Directory holding a venv's executables (``Scripts`` / ``bin``).

    Canonical helper for venv layout. This was open-coded in seven places
    across four ``hermes_cli`` modules using three different Windows
    predicates (``platform.system()``, ``is_windows()``, ``_is_windows()``);
    each new call site had to re-derive it, and #76091 shipped an eighth copy
    because the correct behaviour lived 2400 lines away in another function.
    A few sites outside ``hermes_cli`` (``tools/code_execution_tool.py``,
    ``agent/lsp/install.py``, ``agent/lsp/servers.py``) still hand-roll it —
    convert them as they are touched.

    *windows* lets a caller pass its own platform verdict. Several callers
    resolve this through predicates the test-suite patches to exercise
    Windows paths on Linux CI (``hermes_cli.main._is_windows`` and friends);
    reading ``sys.platform`` unconditionally here would silently drop those
    paths out of coverage. Defaults to the host platform.

    The path is returned unconditionally — callers legitimately differ on
    whether a missing venv is an error, so existence checking stays with them.
    """
    if windows is None:
        windows = sys.platform == "win32"
    return Path(venv_dir) / ("Scripts" if windows else "bin")


def project_venv_dir(project_root) -> Path | None:
    """The project's venv directory, ``venv`` or ``.venv``, when one exists.

    ``uv venv`` defaults to ``.venv`` while our installers create ``venv``, so
    both layouts are in the wild. Call sites that only knew about ``venv``
    silently no-oped on a ``.venv`` install — that is how the Windows
    shim-lock preflight skipped itself entirely (#79542). ``venv`` wins when
    both exist, matching what the installers write.
    """
    for name in ("venv", ".venv"):
        candidate = Path(project_root) / name
        if candidate.is_dir():
            return candidate
    return None


def venv_python_path(venv_dir, *, windows: bool | None = None) -> Path:
    """Path to the Python interpreter inside *venv_dir* (may not exist)."""
    if windows is None:
        windows = sys.platform == "win32"
    return venv_bin_dir(venv_dir, windows=windows) / (
        "python.exe" if windows else "python"
    )


# ─── Partial-update diagnostics ──────────────────────────────────────────────

# Top-level packages/modules that ship as part of Hermes itself. An ImportError
# naming one of these means our own tree is inconsistent; anything else is a
# third-party problem with different remediation. Single source of truth —
# `hermes_cli.update_cmd`'s post-update probe consumes this same set so the
# guard that BLOCKS and the hint that EXPLAINS can never disagree.
FIRST_PARTY_MODULE_ROOTS = frozenset(
    {
        "agent",
        "acp_adapter",
        "cli",
        "cron",
        "gateway",
        "model_tools",
        "plugins",
        "providers",
        "tools",
        "toolsets",
        "run_agent",
        "tui_gateway",
        "utils",
    }
)


def is_first_party_module(name: str | None) -> bool:
    """True when *name* is a module that ships with Hermes.

    Matches on the first dotted segment against an exact set — a substring or
    ``startswith`` test would also claim third-party ``agents``, ``agentops``,
    and ``toolsets_x``.
    """
    root = str(name).split(".")[0] if name else ""
    if not root:
        return False
    return root in FIRST_PARTY_MODULE_ROOTS or root.startswith("hermes_")


def partial_update_hint(exc: BaseException) -> list[str]:
    """Return recovery guidance lines when *exc* looks like a half-updated tree.

    An interrupted or partially-applied update can leave the checkout with new
    files in one package and stale files in another. Every file still parses,
    so nothing is corrupt in the usual sense — but a module that imports a name
    added in the same release from a sibling that wasn't refreshed dies with
    ``ImportError: cannot import name 'X' from 'y'`` on every startup.

    Users hit this as an opaque crash with no indication that the *install*,
    rather than their config, is the problem — and `hermes update` is exactly
    the command they need but are least likely to trust after a failed update.
    Return the guidance so callers can print it alongside the raw error.

    Returns an empty list for unrelated exceptions, so callers can splat it
    unconditionally.
    """
    if not isinstance(exc, ImportError):
        return []
    # A missing third-party dependency is a different problem (bad venv, missing
    # extra) with different remediation, so don't claim a partial update.
    if isinstance(exc, ModuleNotFoundError):
        return []
    name = getattr(exc, "name", None)
    if not is_first_party_module(name):
        return []
    return [
        "",
        "This looks like a partially-updated install: one module was refreshed "
        "and a related one was not.",
        "Re-run the update to bring the whole tree to the same version:",
        "    hermes update",
        "If that also fails, reinstall: https://hermes-agent.nousresearch.com",
    ]


# ─── compat surface for updaters that are already in the wild ───────────
#
# `hermes update` runs in the PRE-update process against the POST-update
# files: every lazy import it performs after the tree swap reads the new
# tree. A name an old updater imports there cannot be deleted without
# breaking that update, mid-flight, on a half-new checkout.
#
# `scripts/audit-old-updater-imports.py` derives the full list from every
# shipped commit, and `tests/test_old_updater_compat_surface.py` fails if
# one of these disappears. Do not delete anything below without checking
# that no released updater still imports it.


def with_hermes_node_path(env: "dict[str, str] | None" = None) -> "dict[str, str]":
    """Return *env* with the managed runtime dirs prepended to PATH.

    COMPAT ONLY. Current code calls
    ``installation.env.with_managed_runtimes`` directly; this name stays
    because 48 shipped versions of ``hermes update`` import it lazily
    while updating (``update_cmd`` uses it for the NixOS build env and
    the Node dependency step), so deleting it breaks those updates at
    the moment the tree is half-replaced.

    It now forwards to the runtime registry rather than keeping its own
    copy of the PATH rules: the old body walked a hardcoded
    ``$HERMES_HOME/node`` layout that no longer exists.
    """
    from installation.env import with_managed_runtimes

    return with_managed_runtimes(env)
