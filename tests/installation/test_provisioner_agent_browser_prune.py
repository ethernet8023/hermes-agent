"""agent-browser stages one binary, not seven.

The upstream registry tarball ships every platform's driver side by side
in ``bin/`` (~69MB). A sealed payload runs exactly the one
``_binary_rel`` names, so staging prunes the rest. The contract under
test: the target's own binary survives, no foreign-arch binary reaches
the store, and a tarball whose layout stopped matching fails loudly
rather than pruning everything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from installation.provisioner import (
    _binary_rel,
    _prune_foreign_agent_browser_binaries,
)

# The bin/ layout of agent-browser 0.26.0, verbatim.
TARBALL_BINARIES = (
    "agent-browser-darwin-arm64",
    "agent-browser-darwin-x64",
    "agent-browser-linux-arm64",
    "agent-browser-linux-musl-arm64",
    "agent-browser-linux-musl-x64",
    "agent-browser-linux-x64",
    "agent-browser-win32-x64.exe",
)
NON_BINARIES = ("agent-browser.js", ".install-method")

# win32-arm64 is a declared gap in the pin table, so it never stages.
STAGED_TARGETS = ("linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64")


def _stage_tarball_layout(dest: Path) -> Path:
    bin_dir = dest / "bin"
    bin_dir.mkdir(parents=True)
    for name in TARBALL_BINARIES + NON_BINARIES:
        (bin_dir / name).write_bytes(b"\x7fELF placeholder")
    return bin_dir


@pytest.mark.parametrize("target", STAGED_TARGETS)
def test_only_the_target_binary_survives(tmp_path: Path, target: str) -> None:
    bin_dir = _stage_tarball_layout(tmp_path)

    _prune_foreign_agent_browser_binaries(tmp_path, target)

    survivors = {p.name for p in bin_dir.iterdir() if p.name.startswith("agent-browser-")}
    assert survivors == {Path(_binary_rel("agent-browser", target)).name}


@pytest.mark.parametrize("target", STAGED_TARGETS)
def test_the_surviving_binary_is_the_one_the_fact_records(
    tmp_path: Path, target: str
) -> None:
    """The pruner and the fact must name the same file.

    Pruning by a rule that disagrees with ``_binary_rel`` would delete
    the binary the store is about to record, so assert they agree
    rather than restating the expected filename.
    """
    _stage_tarball_layout(tmp_path)

    _prune_foreign_agent_browser_binaries(tmp_path, target)

    assert (tmp_path / _binary_rel("agent-browser", target)).is_file()


def test_shim_and_sidecars_are_kept(tmp_path: Path) -> None:
    bin_dir = _stage_tarball_layout(tmp_path)

    _prune_foreign_agent_browser_binaries(tmp_path, "linux-x64")

    for name in NON_BINARIES:
        assert (bin_dir / name).is_file()


def test_a_changed_upstream_layout_fails_instead_of_emptying_bin(
    tmp_path: Path,
) -> None:
    """No target's binary present means the naming convention moved.

    Silently pruning there would leave a bin/ with nothing runnable in
    it and defer the failure to the store's run-the-binary probe, well
    past the point where the cause is readable.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    for name in TARBALL_BINARIES:
        (bin_dir / name.replace("agent-browser-", "agentbrowser_")).write_bytes(b"x")

    with pytest.raises(RuntimeError, match="upstream bin/ layout changed"):
        _prune_foreign_agent_browser_binaries(tmp_path, "linux-x64")

    assert list(bin_dir.iterdir())


def test_a_missing_bin_dir_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="without a bin/ directory"):
        _prune_foreign_agent_browser_binaries(tmp_path, "linux-x64")
