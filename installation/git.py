"""The ONE git locator, and what to tell a user who has no git.

Git is the odd tool in the pin table. Every other managed tool is
downloaded on every platform; git is not:

* **Windows** always uses the managed PortableGit. ``bash.exe`` ships
  inside it, and a system git's bash can be missing or ASLR-broken, so
  the managed copy is not a preference there — it is the contract.
* **macOS and Linux** use the machine's git when it clears the flag
  floor, and otherwise have NONE. A 147 MB dugite download to run
  ``git rev-parse`` is the wrong trade on a platform where git is one
  package-manager command away, and on macOS the system path is worse
  than absent: ``/usr/bin/git`` is the xcode-select SHIM, a stub whose
  only behaviour is to pop a modal "install developer tools?" dialog.
  A background process must never trigger that.

So ``git_path()`` can return None, and every caller must handle it.
That is the whole point of this module: three call sites used to each
decide for themselves what "no git" meant, and each one decided
differently. One returned the shim. One fell back to a bare ``git``
argv that the shim would answer. One reported success.

``git_install_guidance()` gives the user the platform-correct fix so a
caller can report a real next step instead of "git not found".
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

__all__ = [
    "SYSTEM_GIT_FLOOR",
    "git_install_guidance",
    "git_path",
    "probe_system_git",
]

#: The newest git flag this codebase builds decides the floor.
#: ``scripts/audit-git-flags.py`` derives it: a system git below this
#: accepts the ``--version`` probe and then fails mid-operation on a real
#: call, which is a worse failure than having no git at all.
SYSTEM_GIT_FLOOR = (2, 31)


def _is_xcode_shim(binary: str | Path | None) -> bool:
    """True when *binary* is the macOS developer-tools stub."""
    try:
        from installation.env import is_macos_xcode_shim

        return is_macos_xcode_shim(binary)
    except Exception:  # noqa: BLE001 — a probe must not take a caller down
        return False


def _probe_version(binary: Path | str) -> Optional[str]:
    """``git --version`` as ``X.Y.Z``, or None when it does not answer."""
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # "git version 2.53.0" and "git version 2.39.5 (Apple Git-154)"
    parts = (result.stdout or "").split()
    for token in parts:
        if token and token[0].isdigit():
            return token
    return None


def _meets_floor(version: str) -> bool:
    numbers = version.split(".")
    try:
        pair = (int(numbers[0]), int(numbers[1]))
    except (ValueError, IndexError):
        return False
    return pair >= SYSTEM_GIT_FLOOR


def probe_system_git() -> Optional[tuple[str, str]]:
    """A usable machine-provided git: ``(absolute_path, version)`` or None.

    Usable means on PATH, not the xcode-select shim, answers
    ``--version``, and meets :data:`SYSTEM_GIT_FLOOR` so every flag this
    codebase passes will be understood.

    POSIX-only by design: Windows always takes the managed PortableGit.
    """
    if sys.platform == "win32":
        return None
    found = shutil.which("git")
    if found is None or _is_xcode_shim(found):
        return None
    version = _probe_version(found)
    if version is None or not _meets_floor(version):
        return None
    return found, version


def git_path() -> Optional[Path]:
    """The git this install should run, or None when there is none.

    Resolution order:

    1. The managed git, when the provisioner recorded one. On Windows
       this is the only accepted answer.
    2. On macOS and Linux, a system git that clears the floor and is not
       the xcode shim.
    3. None.

    None is a normal answer on macOS and Linux, not an error state. Pair
    it with :func:`git_install_guidance` when reporting to a user.
    """
    try:
        from installation.env import managed_tool_binary

        managed = managed_tool_binary("git")
    except Exception:  # noqa: BLE001 — a lookup must not take a caller down
        managed = None
    if managed is not None:
        return Path(managed)

    if sys.platform == "win32":
        # No managed PortableGit means no bash.exe either. A system git
        # here is not equivalent, so report the gap instead of hiding it.
        return None

    probed = probe_system_git()
    if probed is None:
        return None
    return Path(probed[0])


def git_install_guidance() -> str:
    """One line telling this platform's user how to get git."""
    if sys.platform == "darwin":
        return (
            "Install git with `xcode-select --install` (Command Line Tools) "
            "or `brew install git`."
        )
    if sys.platform == "win32":
        return (
            "The managed git is missing. Run "
            "`python -m installation.provisioner` to restore it."
        )
    return (
        "Install git with your package manager, for example "
        "`sudo apt install git`, `sudo dnf install git`, or "
        "`sudo pacman -S git`."
    )
