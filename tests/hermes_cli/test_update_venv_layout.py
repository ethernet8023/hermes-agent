"""``update_cmd``'s venv discovery must find a ``.venv`` install.

Three sites hardcoded ``PROJECT_ROOT / "venv"``:

- ``_venv_core_imports_healthy`` — the post-update health probe that exists
  so a half-finished dependency sync cannot leave the user printing
  "Already up to date!" forever (ryanc's incident, July 2026).
- two process-holder scans that find what is keeping the venv open before
  the updater replaces it.

On a uv-default ``.venv`` checkout none of them found the venv, so the
health probe silently reported "no venv interpreter -> healthy" and the
scans reported nothing holding a venv they were not looking at. They now
ask ``managed_uv.resolve_live_venv``, the one resolver that knows both
layouts and probes for a real interpreter rather than a directory.
"""

import os
from pathlib import Path

import pytest

from hermes_cli import update_cmd
from hermes_cli.managed_uv import resolve_live_venv


def _make_venv(root: Path, name: str) -> Path:
    """A checkout whose venv is named *name* and holds an interpreter."""
    bin_dir = root / name / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True)
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    return root / name


class TestUpdateFindsEitherVenvLayout:
    @pytest.fixture
    def checkout(self, tmp_path, monkeypatch):
        """Point update_cmd's PROJECT_ROOT at a scratch checkout."""
        root = tmp_path / "checkout"
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

        import hermes_cli.main as main_mod

        monkeypatch.setattr(main_mod, "PROJECT_ROOT", root)
        return root

    @pytest.mark.parametrize("layout", ["venv", ".venv"])
    def test_health_probe_targets_the_venv_that_exists(
        self, checkout, layout, monkeypatch
    ):
        venv = _make_venv(checkout, layout)
        probed = {}

        # The probe runs the venv's interpreter; capture which one it picked
        # instead of executing anything.
        def fake_run(cmd, *a, **kw):
            probed["python"] = cmd[0]
            raise RuntimeError("stop after resolution")

        monkeypatch.setattr(update_cmd.subprocess, "run", fake_run)

        healthy, _detail = update_cmd._venv_core_imports_healthy()

        assert str(venv) in probed["python"], probed
        # A probe that blew up must not be reported as a broken install.
        assert healthy is True

    def test_dot_venv_only_checkout_is_not_treated_as_venvless(self, checkout):
        # The regression: with only .venv present the old code resolved to a
        # non-existent <root>/venv, and every caller took its "no venv here"
        # branch.
        venv = _make_venv(checkout, ".venv")
        assert resolve_live_venv(checkout) == venv
        assert resolve_live_venv(checkout).exists()

    def test_venv_wins_when_both_layouts_are_present(self, checkout):
        managed = _make_venv(checkout, "venv")
        _make_venv(checkout, ".venv")
        # Deterministic precedence: the managed layout the installer creates.
        assert resolve_live_venv(checkout) == managed
