"""Doctor reports the tools Hermes would actually run — the pm store first.

Hermes runs pinned tools out of the pm store. Nothing puts the store on an
interactive shell's PATH, so a PATH-only probe reports a perfectly healthy
managed install as "not found", and silently skips the checks that depend
on the tool being present.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import doctor


def test_managed_tool_wins_over_a_system_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pm-store binary is what Hermes runs, so it is what doctor reports."""
    managed = tmp_path / "store" / "node-1.0.0" / "node"
    managed.parent.mkdir(parents=True)
    managed.write_text("", encoding="utf-8")
    monkeypatch.setattr(doctor, "_pm_tool_path", lambda _tool: managed)
    monkeypatch.setattr(doctor, "_safe_which", lambda _cmd: "/usr/bin/node")

    resolved, source = doctor._managed_pm_tool("node")

    assert resolved == str(managed)
    assert source == "managed"


def test_ripgrep_probes_path_as_rg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pm package is named 'ripgrep' but the PATH command is 'rg'."""
    seen = {}

    def which(cmd):
        seen["cmd"] = cmd
        return "/usr/bin/rg"

    monkeypatch.setattr(doctor, "_pm_tool_path", lambda _tool: None)
    monkeypatch.setattr(doctor, "_safe_which", which)

    resolved, source = doctor._managed_pm_tool("ripgrep")

    assert seen["cmd"] == "rg"
    assert resolved == "/usr/bin/rg"
    assert source == "system"


def test_a_managed_install_is_not_reported_as_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bug this replaced: managed tool present, nothing on PATH.

    A PATH-only probe called this "not found" and skipped the checks that
    run on an install that works perfectly well.
    """
    managed = tmp_path / "store" / "node-1.0.0" / "node"
    managed.parent.mkdir(parents=True)
    managed.write_text("", encoding="utf-8")
    monkeypatch.setattr(doctor, "_pm_tool_path", lambda _tool: managed)
    monkeypatch.setattr(doctor, "_safe_which", lambda _cmd: None)

    resolved, _ = doctor._managed_pm_tool("node")

    assert resolved is not None


def test_system_copy_is_still_reported_when_unstaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor reports what is on the machine, so PATH stays the second rung."""
    monkeypatch.setattr(doctor, "_pm_tool_path", lambda _tool: None)
    monkeypatch.setattr(doctor, "_safe_which", lambda _cmd: "/usr/bin/node")

    resolved, source = doctor._managed_pm_tool("node")

    assert resolved == "/usr/bin/node"
    assert source == "system"


def test_a_broken_pm_read_degrades_to_the_path_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pm-store read failure must not take doctor down — PATH still answers."""

    def _boom(_tool):
        raise RuntimeError("facts.json unreadable")

    monkeypatch.setattr(doctor, "_pm_tool_path", _boom)
    monkeypatch.setattr(doctor, "_safe_which", lambda _cmd: "/usr/bin/node")

    resolved, source = doctor._managed_pm_tool("node")

    assert resolved == "/usr/bin/node"
    assert source == "system"


def test_absent_everywhere_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No managed tool and nothing on PATH is a real "not found"."""
    monkeypatch.setattr(doctor, "_pm_tool_path", lambda _tool: None)
    monkeypatch.setattr(doctor, "_safe_which", lambda _cmd: None)

    resolved, _ = doctor._managed_pm_tool("node")

    assert resolved is None


class TestPmToolPath:
    """_pm_tool_path answers from facts.json + the pm store, nothing else."""

    def _store(self, tmp_path, monkeypatch, facts: dict):
        import json

        store = tmp_path / "tools"
        store.mkdir(parents=True, exist_ok=True)
        (store / "facts.json").write_text(
            json.dumps({"schema": 1, "packages": facts}), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(store))
        return store

    def test_staged_tool_resolves_to_its_store_binary(self, tmp_path, monkeypatch):
        import sys

        store = self._store(
            tmp_path, monkeypatch,
            {"ripgrep": {"entry": "ripgrep-15.0.0-test", "version": "15.0.0", "env": {}}},
        )
        entry = store / "ripgrep-15.0.0-test"
        entry.mkdir()
        binary = entry / ("rg.exe" if sys.platform == "win32" else "rg")
        binary.write_text("", encoding="utf-8")

        assert doctor._pm_tool_path("ripgrep") == binary

    def test_unstaged_tool_resolves_to_none(self, tmp_path, monkeypatch):
        self._store(tmp_path, monkeypatch, {})
        assert doctor._pm_tool_path("ripgrep") is None

    def test_recorded_but_deleted_binary_resolves_to_none(self, tmp_path, monkeypatch):
        self._store(
            tmp_path, monkeypatch,
            {"ripgrep": {"entry": "ripgrep-15.0.0-test", "version": "15.0.0", "env": {}}},
        )
        assert doctor._pm_tool_path("ripgrep") is None
