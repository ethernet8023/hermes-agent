"""The driver and the engine are separate questions.

The check this module replaced asked one question about both::

    _agent_browser_resolves() or _has_system_browser()

so a machine with Chrome on PATH and no driver answered True, the lazy
path skipped the provision it existed to run, and the caller raised
"agent-browser CLI not found". Having a browser installed SUPPRESSED
the browser install. These tests pin the split.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from installation import browser


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """Answer driver/engine lookups from a fake managed tree."""
    tools: dict[str, Path] = {}

    def fake_managed(tool: str):
        return tools.get(tool)

    monkeypatch.setattr(browser, "_managed", fake_managed)
    monkeypatch.delenv(browser.ENGINE_OVERRIDE_ENV, raising=False)

    def stage(tool: str) -> Path:
        binary = tmp_path / tool
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        tools[tool] = binary
        return binary

    return stage


def test_a_system_chrome_does_not_answer_for_the_driver(staged, tmp_path, monkeypatch):
    """The regression. A PATH Chrome must not report the driver present.

    This is the exact shape that made the old check lie: the machine has
    a perfectly good browser under every name the old probe searched,
    and no agent-browser at all.
    """
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        binary = tmp_path / name
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert browser.driver_path() is None


def test_a_system_chrome_does_not_answer_for_the_engine(staged, tmp_path, monkeypatch):
    """Nor for the engine: an unpinned Chrome is not the pinned pair."""
    chrome = tmp_path / "google-chrome"
    chrome.write_text("#!/bin/sh\n")
    chrome.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert browser.engine_path() is None


def test_driver_path_reports_the_staged_driver(staged):
    binary = staged(browser.DRIVER_TOOL)
    assert browser.driver_path() == binary


def test_engine_path_prefers_the_explicit_override(staged, tmp_path, monkeypatch):
    """Docker resolves its baked-in Chromium into this variable at boot."""
    staged("chromium")
    override = tmp_path / "docker-chromium"
    override.write_text("#!/bin/sh\n")
    monkeypatch.setenv(browser.ENGINE_OVERRIDE_ENV, str(override))

    assert browser.engine_path() == override


def test_engine_path_ignores_an_override_that_is_not_there(staged, tmp_path, monkeypatch):
    """A stale override falls through to the pin rather than winning."""
    pinned = staged("chromium")
    monkeypatch.setenv(browser.ENGINE_OVERRIDE_ENV, str(tmp_path / "deleted-browser"))

    assert browser.engine_path() == pinned


@pytest.mark.parametrize("tool", browser.ENGINE_TOOLS)
def test_either_pinned_engine_answers(staged, tool):
    binary = staged(tool)
    assert browser.engine_path() == binary


def test_the_driver_and_its_engines_are_all_pinned():
    """Every tool this module names must exist in the pin table.

    A name that is not pinned can never be provisioned: provision_tool
    returns "<tool> is not pinned" and the install silently fails.
    """
    from installation.registry import load_pins

    pins = load_pins()
    for tool in (browser.DRIVER_TOOL, *browser.ENGINE_TOOLS):
        assert tool in pins, f"{tool!r} is not in the pin table"


def test_provisioning_the_driver_brings_up_its_engine():
    """The closure walk is what makes one provision enough.

    ``provision_driver`` stages only the driver by name; the engine pair
    arrives because the pin table records it as a ``requires`` edge. If
    that edge is ever dropped, a staged driver has no browser to drive.
    """
    from installation.provisioner import requires_closure
    from installation.registry import load_pins

    pins = load_pins()
    closure = requires_closure(browser.DRIVER_TOOL, pins)
    for engine in browser.ENGINE_TOOLS:
        assert engine in closure, (
            f"{engine!r} left the agent-browser requires closure — a lazy "
            "driver install would no longer bring up a browser"
        )
