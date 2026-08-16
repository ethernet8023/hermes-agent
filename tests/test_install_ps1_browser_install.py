"""Regression tests for install.ps1 browser setup.

History: Install-AgentBrowser once eagerly npm-installed agent-browser and
later @askjo/camofox-browser@^1.5.2 (PR #44772 review trimmed the former).
Commit 0583e3a720 ("pin the browser BINARY; the npm module gets a lockfile")
then removed the function entirely: browser provisioning became a pinned-
binary concern (camoufox in the pin table / provisioner), and the default
browser backend on Windows is Install-BrowserUseCli (uv tool install).

These tests guard the properties that outlived the refactor:
- no eager npm install of agent-browser anywhere in the installer,
- the browser-use install stays managed-first and inside Hermes' bin dir,
- system-browser detection survives.

Linux CI cannot execute the PowerShell installer, so verification here is
source-text-level only, matching tests/test_install_sh_browser_install.py
and tests/test_install_ps1_ascii_only.py.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"


def _extract_function_body(source: str, name: str) -> str:
    m = re.search(
        rf"^function {re.escape(name)} \{{.*?^\}}", source, re.MULTILINE | re.DOTALL
    )
    assert m, f"could not extract function {name} from install.ps1"
    return m.group(0)


def test_agent_browser_is_never_eagerly_npm_installed() -> None:
    """agent-browser resolves lazily via npx (tools/browser_tool.py); the
    installer must not reintroduce an eagerly npm-installed, separately
    version-pinned copy of it — in any function, since Install-AgentBrowser
    itself is gone."""
    text = INSTALL_PS1.read_text()

    assert "Install-AgentBrowser" not in text  # the whole function was removed
    assert "agent-browser@" not in text
    assert "Installing Chromium via agent-browser install" not in text
    assert "agent-browser.cmd" not in text


def test_camofox_npm_module_is_not_installed_by_the_installer() -> None:
    """0583e3a720 moved camoufox acquisition to the pinned-binary provisioner
    (runtime-pins.json optional tool) precisely because the npm module's
    caret range and release-scraping postinstall were unpinned. The installer
    must not resurrect the npm path."""
    text = INSTALL_PS1.read_text()

    assert "@askjo/camofox-browser" not in text


def test_browser_use_cli_install_is_managed_first_and_hardened() -> None:
    """The default browser backend install: only Hermes' managed copy
    short-circuits (a PATH browser-use is a side install), the binary lands
    in Hermes' managed bin via UV_TOOL_BIN_DIR, and user-level uv config
    cannot redirect it (UV_NO_CONFIG)."""
    body = _extract_function_body(INSTALL_PS1.read_text(), "Install-BrowserUseCli")

    assert "UV_TOOL_BIN_DIR" in body
    assert "UV_NO_CONFIG" in body
    assert "browser-use.exe" in body  # managed-copy short-circuit probe
    assert "Get-Command" not in body.split("MANAGED-FIRST")[-1].split("Write-Info")[0]


def test_system_browser_detection_survives() -> None:
    """System-browser detection is still cheap/valuable without agent-browser;
    the helpers must remain defined and called."""
    text = INSTALL_PS1.read_text()

    assert "function Find-SystemBrowser" in text
    assert "function Write-BrowserEnv" in text
