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


def test_install_ps1_derives_path_from_runtime_facts_not_hardcoded_node_dir() -> None:
    """#93170 correction: after provisioning, PATH comes from the shared
    runtime-facts authority (installation.env.managed_path_dirs), never a
    literal ``.hermes-runtime\\node`` prepend that drifts from the pinned
    node (which may live in the shared tool store)."""
    text = _install_ps1()
    # The old defect is gone: no hard-coded .hermes-runtime\node prepend.
    assert ".hermes-runtime\\node\";$env:Path" not in text
    assert ".hermes-runtime\\node`;$env:Path" not in text
    assert re.search(
        r"\$managedNode = Join-Path \$InstallDir \"\.hermes-runtime\\node\"",
        text,
    ) is None, "hard-coded runtime node dir must not be staged into PATH"
    # The shared authority is invoked and its dirs are folded into PATH.
    assert "installation.env.managed_path_dirs" in text
    assert re.search(
        r"\$env:Path = \"\$d;\$env:Path\"",
        text,
    ), "managed runtime dirs from the facts authority must be prepended to PATH"


def test_stage_node_deps_fails_before_npm_when_provisioning_fails() -> None:
    """#93170 correction: Stage-NodeDeps must stop before any npm work when
    managed-runtime provisioning fails — a system npm must never stand in
    for the pinned one."""
    text = _install_ps1()
    assert re.search(
        r"function Stage-NodeDeps \{[\s\S]{0,400}?if \(-not \(Invoke-RuntimeProvisioning\)\)[\s\S]{0,400}?return",
        text,
    ), "Stage-NodeDeps must gate Install-NodeDeps on Invoke-RuntimeProvisioning success"
    # The old unconditional chaining is gone.
    assert "Invoke-RuntimeProvisioning; Install-NodeDeps" not in text
