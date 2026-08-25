"""installation.git: one locator, and what "no git" means per platform.

Git is the one pinned tool that is NOT downloaded everywhere. Windows
always takes the managed PortableGit (bash.exe ships inside it). macOS
and Linux take a system git that clears the flag floor, and otherwise
have none — a 147 MB download to run ``git rev-parse`` is the wrong
trade where git is one package-manager command away.

So ``git_path()`` returning None is a NORMAL answer, and the faults this
file guards are the three ways a caller used to get that wrong:

1. returning the macOS xcode-select shim, which pops a modal dialog
2. accepting a git below the flag floor, which passes ``--version`` and
   then fails on a real call
3. reporting "found" from a bare PATH probe when neither holds
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from installation import git as gitmod


def _fake_git(tmp_path: Path, version: str = "2.53.0") -> Path:
    """A stand-in git that answers --version and nothing else."""
    import stat

    path = tmp_path / "git"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write('git version {version}\\n')\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class TestManagedGitWins:
    def test_managed_git_is_preferred(self, tmp_path):
        managed = tmp_path / "managed-git"
        managed.write_text("")
        with patch(
            "installation.env.managed_tool_binary", return_value=managed
        ):
            assert gitmod.git_path() == managed

    def test_windows_without_managed_git_has_none(self, tmp_path):
        """On Windows the managed PortableGit is the contract, not a
        preference: bash.exe ships inside it, so a system git is not an
        equivalent substitute."""
        with patch("installation.env.managed_tool_binary", return_value=None), \
             patch.object(gitmod.sys, "platform", "win32"), \
             patch.object(gitmod.shutil, "which", return_value="C:\\\\sys\\\\git.exe"):
            assert gitmod.git_path() is None


class TestShimIsNeverReturned:
    """The macOS xcode-select stub is not git.

    Running it on a machine without the Command Line Tools pops a modal
    install dialog. Background paths (checkpoints, boot bootstrap, the
    plugin installer) must never be able to trigger that, so the shim
    must not escape from any locator.
    """

    def test_probe_rejects_the_shim(self, tmp_path):
        fake = _fake_git(tmp_path)
        with patch.object(gitmod.shutil, "which", return_value=str(fake)), \
             patch.object(gitmod, "_is_xcode_shim", return_value=True):
            assert gitmod.probe_system_git() is None

    def test_git_path_never_returns_the_shim(self, tmp_path):
        fake = _fake_git(tmp_path)
        with patch("installation.env.managed_tool_binary", return_value=None), \
             patch.object(gitmod.sys, "platform", "darwin"), \
             patch.object(gitmod.shutil, "which", return_value=str(fake)), \
             patch.object(gitmod, "_is_xcode_shim", return_value=True):
            assert gitmod.git_path() is None

    def test_a_real_system_git_is_accepted(self, tmp_path):
        fake = _fake_git(tmp_path)
        with patch("installation.env.managed_tool_binary", return_value=None), \
             patch.object(gitmod.sys, "platform", "darwin"), \
             patch.object(gitmod.shutil, "which", return_value=str(fake)), \
             patch.object(gitmod, "_is_xcode_shim", return_value=False):
            assert gitmod.git_path() == Path(fake)


class TestFlagFloor:
    """A git below the floor answers the probe and then fails for real.

    The floor is derived from the newest flag this codebase builds, so
    accepting an older git means a mid-operation failure instead of an
    up-front "no usable git".
    """

    def test_below_floor_is_rejected(self, tmp_path):
        old = _fake_git(tmp_path, version="2.20.1")
        with patch.object(gitmod.shutil, "which", return_value=str(old)), \
             patch.object(gitmod, "_is_xcode_shim", return_value=False):
            assert gitmod.probe_system_git() is None

    def test_at_floor_is_accepted(self, tmp_path):
        at = _fake_git(tmp_path, version="2.31.0")
        with patch.object(gitmod.shutil, "which", return_value=str(at)), \
             patch.object(gitmod, "_is_xcode_shim", return_value=False):
            probed = gitmod.probe_system_git()
        assert probed is not None
        assert probed[1] == "2.31.0"

    def test_apple_git_version_string_parses(self, tmp_path):
        """Apple's git prints `git version 2.39.5 (Apple Git-154)`."""
        apple = _fake_git(tmp_path, version="2.39.5 (Apple Git-154)")
        with patch.object(gitmod.shutil, "which", return_value=str(apple)), \
             patch.object(gitmod, "_is_xcode_shim", return_value=False):
            probed = gitmod.probe_system_git()
        assert probed is not None
        assert probed[1] == "2.39.5"

    def test_a_git_that_does_not_answer_is_rejected(self, tmp_path):
        with patch.object(gitmod.shutil, "which", return_value="/nonexistent/git"), \
             patch.object(gitmod, "_is_xcode_shim", return_value=False):
            assert gitmod.probe_system_git() is None


class TestGuidance:
    def test_every_platform_gets_an_actionable_line(self):
        for platform, expected in (
            ("darwin", "xcode-select"),
            ("linux", "apt"),
            ("win32", "provisioner"),
        ):
            with patch.object(gitmod.sys, "platform", platform):
                guidance = gitmod.git_install_guidance()
            assert expected in guidance, (platform, guidance)


class TestConsumersDegrade:
    """Every consumer must handle None instead of assuming a git."""

    def test_checkpoint_manager_disables_itself(self):
        from tools.checkpoint_manager import CheckpointManager

        manager = CheckpointManager.__new__(CheckpointManager)
        manager.enabled = True
        manager._git_available = None
        manager._checkpointed_dirs = set()

        with patch("installation.git.git_path", return_value=None):
            assert manager.ensure_checkpoint("/tmp") is False
        assert manager._git_available is False

    def test_working_diff_reports_the_fix(self):
        from tools import working_diff

        with patch("installation.git.git_path", return_value=None):
            result = working_diff.collect_working_diff(cwd="/tmp", mode="working")

        assert result["success"] is False
        assert "git is not available" in result["error"]
        # The user gets a next step, not just a complaint.
        assert any(
            hint in result["error"] for hint in ("apt", "brew", "xcode-select", "provisioner")
        ), result["error"]

    def test_mcp_catalog_raises_with_guidance(self):
        from hermes_cli import mcp_catalog

        entry = mcp_catalog.CatalogEntry.__new__(mcp_catalog.CatalogEntry)
        install = type("I", (), {"type": "git", "url": "u", "ref": "r"})()
        object.__setattr__(entry, "install", install)
        object.__setattr__(entry, "name", "demo")

        with patch("installation.git.git_path", return_value=None), \
             pytest.raises(mcp_catalog.CatalogError) as excinfo:
            mcp_catalog._do_git_install(entry)

        assert "git is required" in str(excinfo.value)

    def test_boot_bootstrap_returns_none(self):
        from hermes_cli import boot_bootstrap

        with patch("installation.git.git_path", return_value=None):
            assert boot_bootstrap._git_binary() is None

    def test_plugins_cmd_returns_none(self):
        from hermes_cli import plugins_cmd

        plugins_cmd._resolve_git_executable.cache_clear()
        try:
            with patch("installation.git.git_path", return_value=None):
                assert plugins_cmd._resolve_git_executable() is None
        finally:
            plugins_cmd._resolve_git_executable.cache_clear()


class TestOneLocator:
    """No consumer may grow its own git ladder again.

    Each of these files used to decide for itself what "no git" meant,
    and each decided differently: one returned the shim, one fell back
    to a bare ``git`` argv the shim would answer, one reported success.
    """

    CONSUMERS = (
        "hermes_cli/boot_bootstrap.py",
        "hermes_cli/plugins_cmd.py",
        "hermes_cli/mcp_catalog.py",
        "tools/working_diff.py",
        "tools/checkpoint_manager.py",
    )

    @pytest.mark.parametrize("relpath", CONSUMERS)
    def test_no_bare_which_git(self, relpath):
        import ast

        repo_root = Path(__file__).resolve().parents[2]
        source = (repo_root / relpath).read_text(encoding="utf-8")
        tree = ast.parse(source)

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else None
            )
            if name != "which":
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "git":
                offenders.append(node.lineno)

        assert not offenders, (
            f"{relpath} calls which('git') directly at line(s) {offenders}. "
            "Use installation.git.git_path(), which rejects the xcode shim "
            "and enforces the flag floor."
        )
