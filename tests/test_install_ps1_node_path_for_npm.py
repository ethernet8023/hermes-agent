"""Regression tests for #48130: Windows npm lifecycle scripts need node on PATH.

The desktop installer can resolve ``npm.cmd`` while postinstall hooks fail with
``'node' is not recognized`` because child ``cmd.exe`` processes do not inherit
a PATH that includes ``node.exe``'s directory.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"


def _install_ps1() -> str:
    return INSTALL_PS1.read_text(encoding="utf-8")


def test_install_ps1_defines_ensure_node_exe_on_path_helper() -> None:
    text = _install_ps1()
    assert "function Ensure-NodeExeOnPath" in text
    assert re.search(
        r"\$env:Path\s*=\s*\"\$nodeExeDir;\$env:Path\"",
        text,
    ), "Ensure-NodeExeOnPath must prepend node.exe's directory to PATH"


def test_test_node_prepends_node_dir_before_success() -> None:
    """Provisioning is mandatory now (6a2f155165: 'the pin table is the only
    Node authority'), so the old Test-Node system-Node version gate is gone.
    The surviving property from #48130: the one place that invokes npm
    (Install-NodeDeps) still puts node.exe's directory on PATH first, so
    npm lifecycle scripts that shell out to bare ``node`` resolve it."""
    text = _install_ps1()
    assert "function Test-Node" not in text  # gate removed with the refactor
    assert re.search(
        r"function Install-NodeDeps \{[\s\S]{0,400}?Ensure-NodeExeOnPath",
        text,
    ), "Install-NodeDeps must call Ensure-NodeExeOnPath before invoking npm"


def test_install_node_deps_prepends_node_dir_before_npm() -> None:
    text = _install_ps1()
    assert re.search(
        r"function Install-NodeDeps \{[\s\S]{0,900}?Ensure-NodeExeOnPath[\s\S]{0,900}?Resolve npm explicitly",
        text,
    ), "Install-NodeDeps must call Ensure-NodeExeOnPath before invoking npm"
