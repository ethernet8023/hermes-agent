"""The Windows version-probe fallback: PE VERSIONINFO, not stdout.

The pinned Chromium pair defeats a stdout-only probe from both
directions, measured against the 145.0.7632.6 CfT payload on Windows 11:

* ``chrome.exe`` is GUI-subsystem. ``--version`` OPENS THE BROWSER and
  never exits: the probe burned its whole timeout and left ten stray
  chrome processes behind. Its PE VERSIONINFO reads 145.0.7632.6.
* ``chrome-headless-shell.exe`` is the mirror image: ``--version``
  prints and exits in 0.04s, and its PE VERSIONINFO is EMPTY.

So the probe needs both rungs, the timeout must not swallow the second
one, and the binary that never answers must not be spawned at all.

Everything touching a real binary is windows_only: the fallback only
fires on win32, and faking the host is banned. cmd.exe stands in for
the silent-but-exiting shape -- ``cmd /c rem`` runs fine with empty
stdout and its PE carries a VERSIONINFO resource on every Windows
install. The probe PLAN is pure input-to-output and is tested on every
host, because that is where the hang decision is made.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from installation.provisioner import (
    _probe_version,
    _version_probe_plan,
    _windows_file_version,
)

VERSION_SHAPE = re.compile(r"^\d+(?:\.\d+)+$")


def _system32(exe: str) -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / exe


# ─── the plan: which rungs each binary gets (host-independent) ──────────────


def test_windows_gui_chrome_is_never_spawned() -> None:
    """The hang, prevented at its source: no --version spawn at all."""
    plan = _version_probe_plan(Path(r"C:\p\chrome-win64\chrome.exe"), None, True)
    assert plan.exec_args is None
    assert plan.read_file_version is True


def test_headless_shell_keeps_the_exec_rung() -> None:
    """It answers on stdout, and its PE resource is empty -- exec is the
    only rung that works for the other half of the pinned pair."""
    plan = _version_probe_plan(
        Path(r"C:\p\chrome-headless-shell-win64\chrome-headless-shell.exe"), None, True
    )
    assert plan.exec_args == ["--version"]


def test_posix_chrome_is_spawned_normally() -> None:
    """The hang is a Windows GUI-subsystem property. The POSIX build
    prints and exits, and has no PE resource to fall back to."""
    plan = _version_probe_plan(Path("/opt/chrome-linux64/chrome"), None, False)
    assert plan.exec_args == ["--version"]
    assert plan.read_file_version is False


def test_explicit_args_are_honoured_even_for_chrome() -> None:
    """A caller passing args states that those args terminate."""
    plan = _version_probe_plan(Path(r"C:\p\chrome.exe"), ["--headless", "-v"], True)
    assert plan.exec_args == ["--headless", "-v"]


# ─── the fallback is reachable from a timeout ───────────────────────────────


@pytest.mark.windows_only
def test_timeout_still_reaches_the_file_version() -> None:
    """The regression itself. TimeoutExpired is a SubprocessError, and
    returning on it skipped the resource rung for the one shape that
    needs it -- which is how a healthy chromium payload was reported as
    "provisioned binary does not run" and failed every Windows build.

    `cmd /c pause` reproduces the hang without needing chrome: it waits
    on input that never comes, so the probe times out exactly as
    chrome.exe --version did, and cmd.exe carries a PE resource.
    """
    version = _probe_version(_system32("cmd.exe"), args=["/c", "pause"], timeout=5)
    assert version is not None and VERSION_SHAPE.match(version)


def test_timeout_without_a_file_version_is_still_a_failure() -> None:
    """Falling through must not invent a version out of a dead binary."""
    with patch(
        "installation.provisioner.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=30),
    ), patch("installation.provisioner._windows_file_version", return_value=None):
        assert _probe_version(Path("vendor-tool"), args=["-v"]) is None


# ─── real binaries ──────────────────────────────────────────────────────────


@pytest.mark.windows_only
def test_versioninfo_read_without_execution() -> None:
    version = _windows_file_version(_system32("notepad.exe"))
    assert version is not None and VERSION_SHAPE.match(version)


@pytest.mark.windows_only
def test_silent_exiting_binary_falls_back_to_versioninfo() -> None:
    """Exec succeeds with empty stdout, the probe still returns a
    version -- from the PE resource."""
    version = _probe_version(_system32("cmd.exe"), args=["/c", "rem"])
    assert version is not None and VERSION_SHAPE.match(version)


@pytest.mark.windows_only
def test_missing_binary_is_still_a_failure() -> None:
    """The fallback must not resurrect a binary that cannot even spawn."""
    assert _probe_version(_system32("hermes-does-not-exist.exe")) is None


@pytest.mark.windows_only
def test_resourceless_binary_is_still_a_failure(tmp_path: Path) -> None:
    """Runs-but-no-VERSIONINFO: both rungs miss, the probe stays None.
    A .cmd shim is exactly that shape."""
    shim = tmp_path / "silent.cmd"
    shim.write_text("@rem nothing\r\n", encoding="ascii")
    assert _probe_version(shim) is None
