"""step_expose_cli — the post-update side owns launcher-wrapper repair.

The installers write ~/.local/bin/hermes* once, at install time. This
step rewrites them when they drift (moved checkout, recreated venv,
deleted by hand) and — just as load-bearing — REFUSES to touch a
launcher that belongs to a different install sharing the link dir.
Real files under a temp HOME; no mocks of the things being tested.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import post_update


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    """A venv-shaped install root plus an isolated HOME."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    root = tmp_path / "checkout"
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "python").write_text("#!fake\n")
    (root / "hermes").write_text("# entrypoint\n")
    (root / "run_agent.py").write_text("# agent\n")
    monkeypatch.setenv("HERMES_INSTALL_ROOT", str(root))
    return home, root


class TestExposeCli:
    def test_writes_all_three_wrappers_fresh(self, fake_install):
        home, root = fake_install
        result = post_update.step_expose_cli()
        assert result["ok"] is True
        assert sorted(result["written"]) == ["hermes", "hermes-acp", "hermes-agent"]
        for name in ("hermes", "hermes-agent", "hermes-acp"):
            wrapper = home / ".local" / "bin" / name
            body = wrapper.read_text()
            assert str(root) in body
            assert "PYTHONPATH" in body
            assert os.access(wrapper, os.X_OK)

    def test_second_run_is_a_no_op(self, fake_install):
        post_update.step_expose_cli()
        result = post_update.step_expose_cli()
        assert result == {"ok": True, "written": []}

    def test_repairs_a_stale_same_install_wrapper(self, fake_install):
        home, root = fake_install
        post_update.step_expose_cli()
        wrapper = home / ".local" / "bin" / "hermes"
        wrapper.write_text(f'#!/bin/sh\nexec "{root}/venv/bin/python" OLD-SHAPE\n')
        result = post_update.step_expose_cli()
        assert "hermes" in result["written"]
        assert "OLD-SHAPE" not in wrapper.read_text()

    def test_leaves_another_installs_wrapper_alone(self, fake_install):
        home, root = fake_install
        other = "/somewhere/else/checkout"
        wrapper_dir = home / ".local" / "bin"
        wrapper_dir.mkdir(parents=True)
        foreign = f'#!/bin/sh\nexec "{other}/venv/bin/python" "{other}/hermes" "$@"\n'
        (wrapper_dir / "hermes").write_text(foreign)
        result = post_update.step_expose_cli()
        assert (wrapper_dir / "hermes").read_text() == foreign
        assert "hermes" not in result["written"]
        # The other two had no file at all — those ARE written.
        assert sorted(result["written"]) == ["hermes-acp", "hermes-agent"]

    def test_config_gate_disables(self, fake_install, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"cli": {"expose_on_path": False}},
        )
        result = post_update.step_expose_cli()
        assert result == {"ok": True, "skipped": "config-disabled"}

    def test_sealed_tree_without_venv_skips(self, fake_install, monkeypatch, tmp_path):
        bare = tmp_path / "sealed"
        bare.mkdir()
        monkeypatch.setenv("HERMES_INSTALL_ROOT", str(bare))
        result = post_update.step_expose_cli()
        assert result == {"ok": True, "skipped": "no-venv-layout"}

    def test_replaces_a_dangling_symlink_from_old_installs(self, fake_install):
        """#21454: `cat >` used to follow an old symlink into the venv and
        clobber the console script. The step must unlink FIRST."""
        home, root = fake_install
        wrapper_dir = home / ".local" / "bin"
        wrapper_dir.mkdir(parents=True)
        console_script = root / "venv" / "bin" / "hermes"
        console_script.write_text("# real console script\n")
        (wrapper_dir / "hermes").symlink_to(console_script)
        result = post_update.step_expose_cli()
        assert "hermes" in result["written"]
        # The venv console script survives untouched…
        assert console_script.read_text() == "# real console script\n"
        # …and the link-dir entry is now a real file, not a symlink.
        assert not (wrapper_dir / "hermes").is_symlink()

    def test_registered_as_a_home_step(self):
        assert ("expose_cli", post_update.step_expose_cli) in post_update.HOME_STEPS
