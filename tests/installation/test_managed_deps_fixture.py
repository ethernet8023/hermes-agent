"""The managed-deps fixture hands tests a REAL provisioned toolchain.

Without this fixture a test that wants a managed tool has to mock the
resolver, and a mocked resolver cannot catch a resolver that returns the
wrong path. These tests are the proof that the fixture delivers tools
the production lookup actually finds.

They are also the fixture's own regression suite: if provisioning stops
landing tools where ``installation.registry`` looks for them, this fails
here rather than in whatever feature test happened to depend on it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from installation import nodejs


class TestUnprovisionedIsTheDefault:
    """The default must stay unprovisioned.

    Every test gets an empty install root, so a test asserting the
    NotProvisioned contract keeps working without opting out of
    anything. If this ever fails, an autouse fixture started handing
    out tools and those contract tests went quietly green.
    """

    @pytest.mark.no_managed_deps
    def test_node_path_raises_without_the_fixture(self):
        with pytest.raises(nodejs.NotProvisioned):
            nodejs.node_path()


class TestManagedDepsFixture:
    def test_node_resolves_and_runs(self, managed_deps: Path):
        node = nodejs.node_path()
        assert node.exists(), node

        result = subprocess.run(
            [str(node), "--version"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().startswith("v"), result.stdout

    def test_npm_resolves(self, managed_deps: Path):
        npm = nodejs.npm_path()
        assert npm.exists(), npm

    def test_the_resolved_node_is_the_pinned_version(self, managed_deps: Path):
        """The fixture must deliver the PINNED node, not any node.

        A fixture that quietly resolved a system node would make every
        test riding it lie about which toolchain it exercised.
        """
        from installation import registry

        pins = registry.load_pins()
        pinned = pins["node"]["version"]

        result = subprocess.run(
            [str(nodejs.node_path()), "--version"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.stdout.strip().lstrip("v") == pinned, (
            f"fixture gave node {result.stdout.strip()}, pin says {pinned}"
        )

    def test_facts_are_install_scoped(self, managed_deps: Path):
        """The facts live under THIS test's install root.

        The bytes are shared through the session store; the facts are
        not. A test that rewrites facts must not leak into the next.
        """
        facts = managed_deps / ".hermes-runtime" / "runtimes.json"
        assert facts.is_file(), f"no facts at {facts}"
