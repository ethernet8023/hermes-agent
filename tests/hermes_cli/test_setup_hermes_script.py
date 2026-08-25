"""setup-hermes.sh is a wrapper over the shared engine, not a 4th installer.

It used to carry its own uv installer (astral-latest via curl|sh) and
its own dependency tiers. These tests freeze the wrapper shape: pinned
uv from the generated fragment, deps via venv_sync, runtimes via the
provisioner, user state via post_update.
"""

from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = REPO_ROOT / "setup-hermes.sh"


def test_setup_hermes_script_is_valid_shell():
    result = subprocess.run(["bash", "-n", str(SETUP_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_uv_comes_from_the_pin_table_not_astral_latest():
    """The old script piped https://astral.sh/uv/install.sh to sh —
    unpinned and unverified. The wrapper stages the digest-checked pin
    into the tool store like every other install path."""
    content = SETUP_SCRIPT.read_text(encoding="utf-8")

    assert "astral.sh/uv/install.sh" not in content
    assert "UV_PIN_SHA256" in content
    assert "BEGIN GENERATED: bootstrap pins" in content
    assert ".hermes-store-entry.json" in content  # store protocol, marker included


def test_the_engine_owns_deps_runtimes_and_user_state():
    content = SETUP_SCRIPT.read_text(encoding="utf-8")

    assert "hermes_cli.venv_sync" in content
    assert "installation.provisioner" in content
    assert "hermes_cli.post_update" in content
    # No private dependency ladder: one seeding `uv sync` for the fresh
    # venv (before hermes_cli exists) is the only direct dep install.
    assert content.count("uv sync") <= 1
    assert "pip install -e \".[all]\"" not in content
