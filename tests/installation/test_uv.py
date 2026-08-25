"""Tests for installation.uv — the pinned uv's acquisition and lookup.

The seams under test are the ones production imports lazily
(``hermes_cli.update_cmd``, ``hermes_cli.main``, ``tools/lazy_deps.py``,
``tools/browser_use_cli.py``): ``uv_path`` (pure registry lookup),
``ensure_uv`` (converge on the pin table, then resolve), and ``uvx_path``
(the sibling binary in the same store entry).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _staged_uv(tmp_path: Path, name: str = "uv") -> Path:
    """A fake staged uv binary with the execute bit set."""
    binary = tmp_path / "store" / "uv-0.0.0-test" / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\necho uv 0.0.0\n")
    binary.chmod(0o755)
    return binary


class TestUvPath:
    def test_resolves_the_registry_recorded_binary(self, tmp_path):
        from installation import uv as uv_mod

        binary = _staged_uv(tmp_path)
        with patch("installation.registry.tool_path", return_value=binary):
            assert uv_mod.uv_path() == binary

    def test_unprovisioned_is_none(self):
        from installation import uv as uv_mod

        with patch("installation.registry.tool_path", return_value=None):
            assert uv_mod.uv_path() is None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX-only: os.access X_OK reflects the execute bit",
    )
    def test_non_executable_binary_reads_as_unprovisioned(self, tmp_path):
        from installation import uv as uv_mod

        binary = _staged_uv(tmp_path)
        binary.chmod(0o644)
        with patch("installation.registry.tool_path", return_value=binary):
            assert uv_mod.uv_path() is None


class TestEnsureUv:
    """ensure_uv always converges on the pin table, then resolves via the
    registry — a failed convergence must not hide a working staged uv."""

    def test_always_runs_the_provisioner(self, tmp_path):
        from installation import uv as uv_mod

        binary = _staged_uv(tmp_path)
        kept = SimpleNamespace(provisioned=True, detail="")
        with patch(
            "installation.provisioner.provision_tool", return_value=kept
        ) as mock_provision, patch(
            "installation.registry.tool_path", return_value=binary
        ):
            result = uv_mod.ensure_uv()

        mock_provision.assert_called_once_with("uv")
        assert result == str(binary)
        assert isinstance(result, str)  # subprocess-argv-safe

    def test_failed_convergence_still_returns_the_staged_binary(self, tmp_path):
        """The old (still working) uv beats no uv when the pin can't be
        converged (offline, digest mismatch)."""
        from installation import uv as uv_mod

        binary = _staged_uv(tmp_path)
        failed = SimpleNamespace(provisioned=False, detail="digest mismatch")
        with patch("installation.provisioner.provision_tool", return_value=failed), \
             patch("installation.registry.tool_path", return_value=binary):
            assert uv_mod.ensure_uv() == str(binary)

    def test_provisioner_exception_is_swallowed(self, tmp_path):
        from installation import uv as uv_mod

        binary = _staged_uv(tmp_path)
        with patch(
            "installation.provisioner.provision_tool",
            side_effect=RuntimeError("network down"),
        ), patch("installation.registry.tool_path", return_value=binary):
            assert uv_mod.ensure_uv() == str(binary)

    def test_nothing_provisioned_anywhere_is_none(self):
        from installation import uv as uv_mod

        failed = SimpleNamespace(provisioned=False, detail="no network")
        with patch("installation.provisioner.provision_tool", return_value=failed), \
             patch("installation.registry.tool_path", return_value=None):
            assert uv_mod.ensure_uv() is None


class TestUvxPath:
    def test_resolves_uvx_beside_uv(self, tmp_path):
        from installation import uv as uv_mod

        binary = _staged_uv(tmp_path)
        uvx = binary.parent / "uvx"
        uvx.write_text("#!/bin/sh\n")
        uvx.chmod(0o755)
        with patch("installation.registry.tool_path", return_value=binary):
            assert uv_mod.uvx_path() == uvx

    def test_windows_named_uv_pairs_with_uvx_exe(self, tmp_path):
        """The .exe suffix follows uv's own spelling, not the host."""
        from installation import uv as uv_mod

        binary = _staged_uv(tmp_path, name="uv.exe")
        uvx = binary.parent / "uvx.exe"
        uvx.write_text("exe")
        uvx.chmod(0o755)
        with patch("installation.registry.tool_path", return_value=binary):
            assert uv_mod.uvx_path() == uvx

    def test_no_uv_means_no_uvx(self):
        from installation import uv as uv_mod

        with patch("installation.registry.tool_path", return_value=None):
            assert uv_mod.uvx_path() is None

    def test_missing_uvx_beside_a_real_uv_is_none(self, tmp_path):
        from installation import uv as uv_mod

        binary = _staged_uv(tmp_path)  # no uvx staged next to it
        with patch("installation.registry.tool_path", return_value=binary):
            assert uv_mod.uvx_path() is None


class TestOldUpdaterCompatShim:
    """``hermes_cli.managed_uv`` survives only as an update-boundary shim:
    a shipped ``hermes update`` imports these names from the NEW tree after
    the checkout swap (frozen in tests/compat/old_updater_surface.json).
    Resolve them statically — the frozen-surface test owns the contract;
    this just keeps the shim's import surface from silently shrinking."""

    def test_frozen_surface_names_resolve(self):
        import hermes_cli.managed_uv as shim

        for name in (
            "ensure_uv",
            "resolve_uv",
            "update_managed_uv",
            "RuntimeRepairResult",
            "repair_vulnerable_runtime",
            "rebuild_venv",
            "_reload_hermes_constants",
        ):
            assert hasattr(shim, name), f"compat shim lost {name}"
