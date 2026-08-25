"""The bootstrap pin fragments must match installation/runtime-pins.json.

The installers (install.sh / install.ps1) are fetched standalone (curl | sh,
irm | iex) and bootstrap uv -- and, on Windows, git -- BEFORE any checkout
exists, so they cannot read the pin table at run time.
scripts/gen-bootstrap-pins.py derives an inline fragment from the table and
splices it between markers in each script. These tests enforce the
derive-don't-store contract: the stored bytes can never drift from the pin
table, and the installers actually consume the pinned values instead of an
unpinned "latest" channel.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "gen-bootstrap-pins.py"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
PINS = REPO_ROOT / "installation" / "runtime-pins.json"


def _pins() -> dict:
    return json.loads(PINS.read_text(encoding="utf-8"))["tools"]


def test_fragments_match_the_pin_table():
    """--check regenerates from the table and fails on any drift."""
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"bootstrap fragments drifted from the pin table:\n"
        f"{result.stdout}{result.stderr}"
    )


def test_installers_carry_the_pinned_uv_version():
    """Both installers hold the exact uv version the pin table names."""
    version = _pins()["uv"]["version"]
    sh = INSTALL_SH.read_text(encoding="utf-8")
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")
    assert f'UV_PIN_VERSION="{version}"' in sh
    assert f'$script:UvPinVersion = "{version}"' in ps1


def test_windows_installer_carries_the_pinned_git():
    """install.ps1's bootstrap git is the pin table's git.

    These used to be independent: install.ps1 hard-coded PortableGit
    2.55.0.3 while the table pinned 2.53.0.3, so a Windows box got one git
    from the installer and a different one from the provisioner.
    """
    git = _pins()["git"]
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")
    assert f'$script:GitPinVersion = "{git["version"]}"' in ps1
    for target in ("win32-x64", "win32-arm64"):
        assert git["files"][target]["url"] in ps1, target
        assert git["files"][target]["sha256"] in ps1, target


def test_windows_git_bootstrap_has_no_hand_written_version():
    """A second git version authority must not come back.

    The literals below are what the drifted copy looked like: a tag and a
    version string built by hand next to the download. Anything matching
    that shape means someone re-introduced a version this test cannot keep
    honest.
    """
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")
    assert "$gitTag" not in ps1
    assert "$gitVer" not in ps1
    # The URL must come from the generated table, not be assembled inline.
    assert 'releases/download/$gitTag' not in ps1


def test_the_self_extracting_git_is_verified_before_it_runs():
    """PortableGit is an .exe: extracting it IS executing it.

    An unverified download here is arbitrary code execution, so the digest
    check must sit between the download and Start-Process.
    """
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")
    hash_at = ps1.index("Get-FileHash -Path $tmpFile")
    extract_at = ps1.index("$extractProc = Start-Process -FilePath $tmpFile")
    assert hash_at < extract_at, "digest check must precede extraction"
    assert "PortableGit digest mismatch" in ps1


def test_installers_do_not_fetch_unpinned_uv():
    """The astral latest-channel installers must never come back.

    astral.sh/uv/install.sh and install.ps1 resolve "latest" at run time,
    which defeats pins-as-repo-data: a hermes install could silently get a
    uv nobody reviewed. The only astral.sh mentions allowed are the manual
    -install docs URL (docs.astral.sh).
    """
    for path in (INSTALL_SH, INSTALL_PS1):
        source = path.read_text(encoding="utf-8")
        assert "astral.sh/uv/install.sh" not in source, path.name
        assert "astral.sh/uv/install.ps1" not in source, path.name
