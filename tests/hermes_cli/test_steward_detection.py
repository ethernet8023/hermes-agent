"""Steward detection reads the install stamp, not a HERMES_HOME marker.

``get_managed_system()`` decides whether a package manager owns this tree
(and therefore whether ``hermes update`` and config writes should refuse).
That is a fact about the INSTALL, so it comes from the install stamp that
ships with the code — the nix package already writes
``distribution: "nix"``. The old ``$HERMES_HOME/.managed`` marker lived in
profile state, so two installs sharing a home saw each other's
stewardship (hermes-home lifetime split, phase 3.9).
"""

import json

import pytest

from hermes_cli.config import get_managed_system, is_managed


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("HERMES_MANAGED", raising=False)
    monkeypatch.delenv("HERMES_INSTALL_ROOT", raising=False)


def _stamp(tmp_path, monkeypatch, **fields):
    """A stamp at a fake install root, resolved the way every steward's
    is: through get_install_root(). No stamp-specific env override
    exists anymore — HERMES_INSTALL_ROOT is the one knob, the same one
    the desktop payload spawn and the nix wrapper set."""
    root = tmp_path / "install"
    root.mkdir(exist_ok=True)
    path = root / "install-stamp.json"
    path.write_text(json.dumps({"commit": "a" * 40, **fields}), encoding="utf-8")
    monkeypatch.setenv("HERMES_INSTALL_ROOT", str(root))
    return path


class TestStampDrivenDetection:
    def test_nix_distribution_is_managed(self, tmp_path, monkeypatch):
        _stamp(tmp_path, monkeypatch, distribution="nix")
        assert get_managed_system() == "NixOS"
        assert is_managed() is True

    def test_source_install_is_not_managed(self, tmp_path, monkeypatch):
        _stamp(tmp_path, monkeypatch, distribution=None, source="local")
        assert get_managed_system() is None
        assert is_managed() is False

    def test_desktop_app_steward_is_not_a_package_manager(self, tmp_path, monkeypatch):
        """A desktop-app stamp says how the tree was DELIVERED. It is not a
        system package manager that owns config writes, so config stays
        writable in the desktop bundle."""
        _stamp(tmp_path, monkeypatch, distribution="desktop-app")
        assert get_managed_system() is None

    def test_docker_steward_is_not_a_package_manager(self, tmp_path, monkeypatch):
        _stamp(tmp_path, monkeypatch, distribution="docker")
        assert get_managed_system() is None

    def test_missing_stamp_is_not_managed(self, monkeypatch, tmp_path):
        empty = tmp_path / "empty-install"
        empty.mkdir()
        monkeypatch.setenv("HERMES_INSTALL_ROOT", str(empty))
        assert get_managed_system() is None

    def test_malformed_stamp_degrades_to_unmanaged(self, tmp_path, monkeypatch):
        root = tmp_path / "install"
        root.mkdir()
        (root / "install-stamp.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("HERMES_INSTALL_ROOT", str(root))
        assert get_managed_system() is None


class TestEnvOverrideStillWins:
    def test_env_beats_stamp(self, tmp_path, monkeypatch):
        _stamp(tmp_path, monkeypatch, distribution=None)
        monkeypatch.setenv("HERMES_MANAGED", "1")
        assert get_managed_system() == "NixOS"

    def test_homebrew_values_stay_ignored(self, monkeypatch):
        monkeypatch.setenv("HERMES_MANAGED", "brew")
        assert get_managed_system() is None


class TestLegacyMarkerIsInert:
    def test_managed_marker_in_hermes_home_is_no_longer_read(
        self, tmp_path, monkeypatch
    ):
        """The marker describes an install but lives in profile state. It
        is left on disk (deleting it is pointless churn) and simply not
        consulted."""
        home = tmp_path / "home"
        home.mkdir()
        (home / ".managed").write_text("", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(home))
        bare = tmp_path / "bare-install"
        bare.mkdir()
        monkeypatch.setenv("HERMES_INSTALL_ROOT", str(bare))

        assert get_managed_system() is None
