"""Tests for the Command Installation check in hermes doctor."""

import sys
import types
from argparse import Namespace
from pathlib import Path

import pytest

import hermes_cli.doctor as doctor_mod


def _stub_doctor_externals(monkeypatch):
    """Stub the imports/network doctor touches so a run stays hermetic."""
    fake_model_tools = types.SimpleNamespace(
        check_tool_availability=lambda *a, **kw: ([], []),
        TOOLSET_REQUIREMENTS={},
    )
    monkeypatch.setitem(sys.modules, "model_tools", fake_model_tools)

    try:
        from hermes_cli import auth as _auth_mod
        monkeypatch.setattr(_auth_mod, "get_nous_auth_status", lambda: {})
        monkeypatch.setattr(_auth_mod, "get_codex_auth_status", lambda: {})
    except Exception:
        pass

    try:
        import httpx
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: types.SimpleNamespace(status_code=200))
    except Exception:
        pass


def _setup_doctor_env(monkeypatch, tmp_path, venv_name="venv"):
    """Create a minimal HERMES_HOME + PROJECT_ROOT for doctor tests."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)

    # Create a fake venv entry point
    venv_bin_dir = project / venv_name / "bin"
    venv_bin_dir.mkdir(parents=True, exist_ok=True)
    hermes_bin = venv_bin_dir / "hermes"
    hermes_bin.write_text("#!/usr/bin/env python\n# entry point\n")
    hermes_bin.chmod(0o755)

    monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor_mod, "_DHH", str(home))

    _stub_doctor_externals(monkeypatch)

    return home, project, hermes_bin


def _run_doctor(fix=False):
    """Run doctor and capture stdout."""
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor_mod.run_doctor(Namespace(fix=fix))
    return buf.getvalue()


class TestDoctorCommandInstallation:
    """Tests for the ◆ Command Installation section."""





    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_fix_repairs_wrong_symlink(self, monkeypatch, tmp_path):
        home, project, hermes_bin = _setup_doctor_env(monkeypatch, tmp_path)

        # Create a symlink pointing to wrong target
        cmd_link_dir = tmp_path / ".local" / "bin"
        cmd_link_dir.mkdir(parents=True)
        cmd_link = cmd_link_dir / "hermes"
        wrong_target = tmp_path / "wrong_hermes"
        wrong_target.write_text("#!/usr/bin/env python\n")
        cmd_link.symlink_to(wrong_target)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = _run_doctor(fix=True)
        assert "Fixed symlink" in out

        # Verify the symlink now points to the correct target
        assert cmd_link.is_symlink()
        assert cmd_link.resolve() == hermes_bin.resolve()

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_missing_venv_entry_point_shows_warn(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")

        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        # Do NOT create any venv entry point

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _stub_doctor_externals(monkeypatch)

        out = _run_doctor(fix=False)
        assert "Command Installation" in out
        assert "Venv entry point not found" in out



    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_termux_uses_prefix_bin(self, monkeypatch, tmp_path):
        """On Termux, the command link dir is $PREFIX/bin."""
        prefix_dir = tmp_path / "termux_prefix"
        prefix_bin = prefix_dir / "bin"
        prefix_bin.mkdir(parents=True)

        home, project, hermes_bin = _setup_doctor_env(monkeypatch, tmp_path)

        monkeypatch.setenv("TERMUX_VERSION", "0.118.3")
        monkeypatch.setenv("PREFIX", str(prefix_dir))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        out = _run_doctor(fix=False)
        assert "Command Installation" in out
        assert "$PREFIX/bin" in out


class TestSealedInstallsSkipCheckoutAdvice:
    """A sealed install has no venv entry point and never should.

    The checkout wiring (``venv/bin/hermes`` + a ``~/.local/bin`` symlink)
    is what install.sh builds. Nix wraps a store binary, the desktop app
    ships its own launcher, Docker puts one on PATH — so probing for the
    checkout layout there reports a problem that does not exist, and the
    remediation told the user to ``pip install -e`` into a read-only store.
    """

    def _sealed(self, monkeypatch, steward):
        from hermes_cli import runtime_tree as rt

        monkeypatch.setattr(
            rt, "runtime_tree",
            lambda root: rt.Sealed(root=Path(root), steward=steward),
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    @pytest.mark.parametrize("steward", ["nix", "docker", "desktop-app"])
    def test_no_pip_advice_and_no_phantom_finding(
        self, monkeypatch, tmp_path, steward
    ):
        # No venv entry point exists — the state that produced the bad advice.
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _stub_doctor_externals(monkeypatch)
        self._sealed(monkeypatch, steward)

        out = _run_doctor(fix=False)

        assert f"launcher provided by {steward}" in out
        assert "Venv entry point not found" not in out
        assert "pip install -e" not in out
        # The venv-activity check is equally inapplicable: the desktop bundle
        # runs outside a venv by design, and Nix's venv is read-only.
        assert "Not in virtual environment" not in out
        assert f"Python environment managed by {steward}" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlink check is Unix-only")
    def test_unknown_steward_still_runs_the_check(self, monkeypatch, tmp_path):
        """Fail open: a missing/corrupt stamp must not silence diagnostics.

        ``runtime_tree`` reports steward "unknown" for any gitless tree
        without a readable stamp — including a broken install, which is
        exactly when doctor needs to speak up.
        """
        home = tmp_path / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("memory: {}\n", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)

        monkeypatch.setattr(doctor_mod, "HERMES_HOME", home)
        monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", project)
        monkeypatch.setattr(doctor_mod, "_DHH", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _stub_doctor_externals(monkeypatch)
        self._sealed(monkeypatch, "unknown")

        out = _run_doctor(fix=False)
        assert "Venv entry point not found" in out

