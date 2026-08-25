"""pm.extras: anchor availability, ensure_import, ensure_and_bind, and the
spec→extra install shim. Network-free — sync_venv is always stubbed (via the
pm.ensure module object; the pm package re-exports the ensure() FUNCTION,
which shadows the submodule attribute for string-path monkeypatching)."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace

import pytest

import pm
import pm.extras as extras

ensure_mod = importlib.import_module("pm.ensure")


@pytest.fixture
def synced(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(ensure_mod, "sync_venv", lambda x=None: calls.append(list(x or [])))
    return calls


def test_available_known_anchor_present():
    assert extras.available("web") is True  # fastapi ships in this venv


def test_available_missing_module():
    assert extras.available("no-such-extra-anywhere") is False


def test_available_counts_sys_modules_fakes(monkeypatch):
    monkeypatch.setitem(sys.modules, "hindsight", SimpleNamespace())
    assert extras.available("hindsight") is True


def test_available_unknown_extra_uses_underscore_guess(monkeypatch):
    monkeypatch.setitem(sys.modules, "some_new_thing", SimpleNamespace())
    assert extras.available("some-new-thing") is True


def test_ensure_import_noop_when_available(monkeypatch, synced):
    monkeypatch.setitem(sys.modules, "fal_client", SimpleNamespace())
    extras.ensure_import("fal")
    assert synced == []


def test_ensure_import_syncs_when_missing(monkeypatch, synced):
    monkeypatch.setattr(extras, "available", lambda e: False)
    extras.ensure_import("fal")
    assert synced == [["fal"]]


def test_ensure_import_propagates_install_error(monkeypatch):
    def boom(x=None):
        raise pm.InstallError("venv", "lazy installs are disabled")

    monkeypatch.setattr(ensure_mod, "sync_venv", boom)
    monkeypatch.setattr(extras, "available", lambda e: False)
    with pytest.raises(pm.InstallError):
        extras.ensure_import("fal")


def test_ensure_and_bind_binds_on_success(monkeypatch, synced):
    monkeypatch.setattr(extras, "available", lambda e: True)
    target: dict = {}
    ok = extras.ensure_and_bind("fal", lambda: {"NAME": 42}, target)
    assert ok is True and target["NAME"] == 42


def test_ensure_and_bind_false_on_install_failure(monkeypatch):
    def boom(x=None):
        raise pm.InstallError("venv", "nope")

    monkeypatch.setattr(ensure_mod, "sync_venv", boom)
    monkeypatch.setattr(extras, "available", lambda e: False)
    target: dict = {}
    assert extras.ensure_and_bind("fal", lambda: {"X": 1}, target) is False
    assert target == {}


def test_ensure_and_bind_false_on_import_failure(monkeypatch, synced):
    monkeypatch.setattr(extras, "available", lambda e: True)

    def importer():
        raise ImportError("still broken")

    assert extras.ensure_and_bind("fal", importer, {}) is False


def test_install_specs_maps_and_dedupes(monkeypatch, synced):
    result = extras.install_extra_for_specs(
        ["honcho-ai==2.2.0", "hindsight-client>=0.6.1", "mem0ai", "fastapi", "uvicorn[standard]"]
    )
    assert result.ok is True
    assert synced == [["hindsight", "honcho", "mem0", "web"]]
    assert "uv sync" in result.command


def test_install_specs_blocked_reason(monkeypatch):
    def boom(x=None):
        raise pm.InstallError("venv", "extras not installed and lazy installs are disabled: ['honcho']")

    monkeypatch.setattr(ensure_mod, "sync_venv", boom)
    result = extras.install_extra_for_specs(["honcho-ai==2.2.0"])
    assert result.ok is False and result.blocked is True
    assert "lazy installs are disabled" in result.reason


def test_install_specs_never_raises(monkeypatch):
    def boom(x=None):
        raise RuntimeError("network fell over")

    monkeypatch.setattr(ensure_mod, "sync_venv", boom)
    result = extras.install_extra_for_specs(["honcho-ai"])
    assert result.ok is False and result.blocked is False
    assert "network fell over" in result.stderr


def test_package_reexports():
    assert pm.available is extras.available
    assert pm.ensure_import is extras.ensure_import


def test_every_anchor_extra_exists_in_pyproject():
    """Contract: ANCHORS maps real pyproject extras (no orphaned names)."""
    import tomllib
    from pathlib import Path

    py = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    declared = set(py["project"]["optional-dependencies"])
    orphans = set(extras.ANCHORS) - declared
    assert not orphans, f"ANCHORS names extras pyproject does not declare: {sorted(orphans)}"
