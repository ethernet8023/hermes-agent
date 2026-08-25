"""Truthful derived build-version metadata for user-facing Hermes displays.

``__version__`` remains the package/API version. This module adds a display
suffix only when it can prove the number of commits since that release.

Resolution order:
1. Install stamp (``install-stamp.json``) — written at build time by
   ``scripts/write_install_stamp.py`` for every packager (Docker, Nix, and
   the desktop app). The stamp is authoritative
   for packaged builds.
2. Live git — for source/dev installs with a ``.git`` directory.
3. Unknown — no stamp and no git. The provenance is unknown.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from hermes_cli import __release_date__, __version__


@dataclass(frozen=True)
class VersionInfo:
    base_version: str
    derived_version: str
    distance: int | None
    commit: str | None
    branch: str | None
    source: Literal["build", "ci", "docker", "fallback", "git", "local", "nix", "unknown"]
    dirty: bool = False
    commit_date: int | None = None
    distribution: Literal["docker", "nix", "desktop-app"] | None = None


def _derived_version(base_version: str, distance: int | None, dirty: bool = False) -> str:
    if distance and distance > 0:
        return f"{base_version}+{distance}"
    if dirty and distance is None:
        return f"{base_version}+?"
    return base_version


def _run_git(repo_dir: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=3, cwd=str(repo_dir)
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or "").strip()
    return value if result.returncode == 0 and value else None


def _resolve_repo_dir() -> Path | None:
    """Use the executing checkout before a profile's optional clone."""
    repo_dir = Path(__file__).parent.parent.resolve()
    if (repo_dir / ".git").exists():
        return repo_dir
    try:
        from hermes_constants import get_hermes_home

        candidate = get_hermes_home() / "hermes-agent"
        if (candidate / ".git").exists():
            return candidate
    except Exception:
        pass
    return None


def _parse_nonnegative(value: str | None) -> int | None:
    try:
        parsed = int(value or "")
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


# --- Install stamp reader ---------------------------------------------------

# The stamp file lives at the install root: beside the code in source
# checkouts and Docker (which writes it to the project root), and in the
# artifact's resources dir for sealed installs — whose processes carry
# HERMES_INSTALL_ROOT (the desktop payload spawn, the Nix wrapper). One
# resolution path for every steward; no stamp-specific env override.
def _resolve_stamp_file() -> Path | None:
    from installation.paths import get_install_root
    from installation.tree import BUILD_INFO_NAME

    p = get_install_root() / BUILD_INFO_NAME
    return p if p.is_file() else None


def _stamp_version_info() -> VersionInfo | None:
    """Read provenance from a build-time install stamp."""
    stamp_file = _resolve_stamp_file()
    if stamp_file is None:
        return None
    try:
        raw = stamp_file.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict) or "commit" not in data:
        return None

    # updateMechanism is required in every stamp — same guard as
    # installation.tree.read_build_info(). A stamp without it means the
    # writing build lane must be fixed, not tolerated.
    if data.get("updateMechanism") not in ("self", "electron-updater", "external"):
        raise RuntimeError(
            f"install-stamp.json at {stamp_file} is missing a valid "
            "'updateMechanism' (one of self, electron-updater, external). The "
            "build lane that wrote this stamp must pass --update-mechanism to "
            "scripts/write_install_stamp.py (or bake the field directly)."
        )
    # A light artifact ships no Python runtime. Reading a light stamp from
    # a Python process means the artifact was mispackaged — same guard as
    # installation.tree.read_build_info().
    if data.get("payload") == "light":
        raise RuntimeError(
            f"install-stamp.json at {stamp_file} marks this artifact as 'light' "
            "(no agent runtime). No Python process can legitimately run from a "
            "light artifact — this build is mispackaged."
        )

    commit = data.get("commit") or None
    if not commit or set(commit) == {"0"}:
        # All-zero placeholder = fallback stamp, not real provenance.
        return None

    base_version = data.get("baseVersion") or __version__
    display_version = data.get("displayVersion") or base_version
    distance = data.get("distance")
    if isinstance(distance, str):
        distance = _parse_nonnegative(distance)

    # ``source`` describes build provenance, while ``distribution`` identifies
    # the package form users installed. Keep both facts intact for support.
    stamp_source = str(data.get("source") or "")
    source = (
        cast(Literal["build", "ci", "docker", "fallback", "git", "local", "nix", "unknown"], stamp_source)
        if stamp_source in {"ci", "docker", "fallback", "local", "nix"}
        else "build"
    )
    distribution = data.get("distribution")
    if distribution not in {"docker", "nix", "desktop-app"}:
        distribution = None

    commit_date = data.get("commitDate")
    if not isinstance(commit_date, int):
        commit_date = None

    return VersionInfo(
        base_version,
        display_version,
        distance if isinstance(distance, int) else None,
        commit,
        data.get("branch") or None,
        source,
        bool(data.get("dirty")),
        commit_date,
        distribution,
    )


# --- Git provenance (source/dev installs) -----------------------------------


def _git_version_info(repo_dir: Path) -> VersionInfo:
    commit = _run_git(repo_dir, "rev-parse", "HEAD")
    # A detached HEAD has no branch. Leave the field None: every formatter
    # already prints the commit separately and handles a missing branch.
    branch = _run_git(repo_dir, "branch", "--show-current")
    commit_date_raw = _run_git(repo_dir, "log", "-1", "--format=%ct", "HEAD")
    commit_date: int | None = None
    if commit_date_raw and commit_date_raw.isdigit():
        commit_date = int(commit_date_raw)
    try:
        # -uno: skip the untracked-file scan. This runs on the startup-banner
        # path, and a full working-tree walk costs real time on large or cold
        # checkouts. Same semantics as write_install_stamp.py.
        dirty_result = subprocess.run(
            ["git", "status", "--porcelain", "-uno"],
            capture_output=True,
            text=True,
            timeout=3,
            cwd=str(repo_dir),
        )
        dirty = dirty_result.returncode == 0 and bool((dirty_result.stdout or "").strip())
    except (OSError, subprocess.SubprocessError):
        dirty = False

    # New releases are SemVer tags. The release-date fallback lets existing
    # CalVer-tagged releases display a correct distance during the transition.
    distance = None
    for tag in (f"v{__version__}", f"v{__release_date__}"):
        raw_distance = _run_git(repo_dir, "rev-list", "--count", f"{tag}..HEAD")
        parsed_distance = _parse_nonnegative(raw_distance)
        if parsed_distance is not None:
            distance = parsed_distance
            break

    return VersionInfo(
        __version__, _derived_version(__version__, distance, dirty), distance, commit, branch, "git", dirty, commit_date
    )


# --- Code identity (fleet version verification) ------------------------------


def _resolve_git_head_sha(project_root: Path) -> str | None:
    """Resolve the checkout's HEAD commit sha by reading .git directly.

    Deliberately NOT ``git rev-parse`` in a subprocess: this helper runs
    inside library paths (gateway runtime-status writes, update receipts)
    where spawning processes is both slow and hostile to tests that mock
    ``subprocess.run`` tightly (call-count asserts, sequenced side effects).
    Handles regular checkouts, worktrees/submodules (``.git`` file with a
    ``gitdir:`` pointer + ``commondir``), loose refs, and packed-refs.
    Returns None on any failure.
    """
    try:
        git_path = project_root / ".git"
        if git_path.is_file():
            # Worktree/submodule: ".git" is a pointer file.
            pointer = git_path.read_text(encoding="utf-8", errors="replace").strip()
            if not pointer.startswith("gitdir:"):
                return None
            git_dir = Path(pointer[len("gitdir:"):].strip())
            if not git_dir.is_absolute():
                git_dir = (project_root / git_dir).resolve()
        elif git_path.is_dir():
            git_dir = git_path
        else:
            return None

        # Refs live in the COMMON git dir for worktrees.
        common_dir = git_dir
        commondir_file = git_dir / "commondir"
        if commondir_file.is_file():
            rel = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
            common = Path(rel)
            if not common.is_absolute():
                common = (git_dir / common).resolve()
            common_dir = common

        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        if not head.startswith("ref:"):
            # Detached HEAD: the file holds the sha itself.
            return head if len(head) == 40 else None
        ref_name = head[len("ref:"):].strip()

        loose = common_dir / ref_name
        if loose.is_file():
            sha = loose.read_text(encoding="utf-8", errors="replace").strip()
            return sha if len(sha) == 40 else None

        packed = common_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "^")):
                    continue
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[1].strip() == ref_name:
                    sha = parts[0].strip()
                    return sha if len(sha) == 40 else None
    except Exception:
        return None
    return None


_code_identity_cache: dict | None = None


def get_code_identity(refresh: bool = False) -> dict:
    """Return the running checkout's code identity as a dict.

    Shape: ``{"sha": full-or-short sha | None, "short_sha": str | None,
    "version": pyproject version | None, "source": "git" | "build-file" |
    "unknown"}``.

    Resolution order: live ``.git`` read for source installs, the install
    stamp for packaged builds (Docker/Nix/desktop ship no ``.git``), else
    unknown. The version is read from ``pyproject.toml`` ON DISK, not the
    imported ``__version__`` — post-update verification must observe the
    NEW code the update wrote, while the running process still carries the
    old modules.

    Cached per process — code identity cannot change while a process is
    running (an updated checkout requires a restart to take effect, which
    is exactly the property the fleet version verification relies on).
    Never raises; every field degrades to ``None`` independently.
    """
    global _code_identity_cache
    if _code_identity_cache is not None and not refresh:
        return dict(_code_identity_cache)

    sha: str | None = None
    source = "unknown"
    project_root = Path(__file__).parent.parent
    resolved = _resolve_git_head_sha(project_root)
    if resolved:
        sha = resolved
        source = "git"
    if sha is None:
        try:
            stamped = _stamp_version_info()
        except Exception:
            stamped = None
        if stamped is not None and stamped.commit:
            sha = stamped.commit
            source = "build-file"

    version: str | None = None
    try:
        import tomllib

        with open(project_root / "pyproject.toml", "rb") as fh:  # windows-footgun: ok — binary mode, tomllib requires bytes
            raw_version = tomllib.load(fh).get("project", {}).get("version")
        version = str(raw_version) if raw_version else None
    except Exception:
        version = None

    _code_identity_cache = {
        "sha": sha,
        "short_sha": sha[:8] if sha else None,
        "version": version,
        "source": source,
    }
    return dict(_code_identity_cache)


# --- Cache + public API -----------------------------------------------------

_cached_version_info: VersionInfo | None = None


def _reset_version_info_cache() -> None:
    """Test-only cache reset."""
    global _cached_version_info
    _cached_version_info = None


def get_version_info() -> VersionInfo:
    """Return cached provenance from install stamp, git, or unknown."""
    global _cached_version_info
    if _cached_version_info is not None:
        return _cached_version_info

    # 1. Install stamp (packaged builds: Docker, Nix)
    info = _stamp_version_info()

    # 2. Live git (source/dev installs)
    if info is None:
        repo_dir = _resolve_repo_dir()
        if repo_dir is not None:
            info = _git_version_info(repo_dir)

    # 3. Unknown — no stamp, no git
    if info is None:
        info = VersionInfo(__version__, __version__, None, None, None, "unknown")

    _cached_version_info = info
    return info
