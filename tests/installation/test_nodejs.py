"""Tests for installation.nodejs — the pinned Node/npm entry points.

These drive the REAL pinned npm against real temp projects. The whole premise
of this module is that the toolchain is guaranteed present and correct, so a
test suite that mocks the toolchain away would assert nothing about the thing
that matters. Installs are offline (a local file: dependency, no registry) so
they stay fast and hermetic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from installation import nodejs, registry


def _provisioned() -> bool:
    return registry.tool_path("npm") is not None and registry.tool_path("node") is not None


needs_toolchain = pytest.mark.skipif(
    not _provisioned(),
    reason="pinned node/npm not provisioned in this environment",
)


def _project(root: Path, *, with_lock: bool, name: str = "probe") -> Path:
    """A minimal installable package with one local file: dependency.

    A file: dependency keeps `npm ci` fully offline: it resolves from disk, so
    the test never reaches the registry but still exercises the real install
    path end to end.
    """
    dep = root / "dep"
    (dep).mkdir(parents=True, exist_ok=True)
    (dep / "package.json").write_text(
        json.dumps({"name": "probe-dep", "version": "1.0.0"}), encoding="utf-8"
    )

    proj = root / name
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "package.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "private": True,
                "dependencies": {"probe-dep": f"file:../dep"},
            }
        ),
        encoding="utf-8",
    )
    if with_lock:
        # Generate a REAL lockfile with the pinned npm rather than hand-writing
        # one: a hand-written lockfile would not match npm's own format and
        # `npm ci` would reject it for the wrong reason.
        result = nodejs.run_npm(
            ["install", "--package-lock-only", "--no-audit", "--no-fund"],
            cwd=proj,
        )
        assert result.returncode == 0, result.stderr
        assert (proj / "package-lock.json").is_file()
    return proj


class TestTheToolchainIsThePinnedOne:
    @needs_toolchain
    def test_node_and_npm_resolve_to_the_runtime_dir(self):
        rt = registry.facts_path().parent
        assert nodejs.node_path().is_relative_to(rt)
        assert nodejs.npm_path().is_relative_to(rt)

    @needs_toolchain
    def test_the_versions_match_the_pin_table(self):
        pins = registry.load_pins()
        node = nodejs.run_node(["--version"])
        npm = nodejs.run_npm(["--version"])
        assert node.stdout.strip().lstrip("v") == pins["node"]["version"]
        assert npm.stdout.strip() == pins["npm"]["version"]

    @needs_toolchain
    def test_the_managed_toolchain_is_assembled_into_the_child_env(self):
        """PATH and the tool env must reach the child, whatever it needs.

        Asserted by reading the env the child actually got, not by watching a
        command succeed: a Nix-built npm has an absolute-path shebang and runs
        with no PATH at all, so "npm --version worked" proves nothing. A
        provisioned npm is `#!/usr/bin/env node` and does need it, and git
        needs GIT_EXEC_PATH regardless of how it was built.
        """
        rt = str(registry.facts_path().parent)
        printer = "console.log(process.env.PATH || '')"
        result = nodejs.run_node(["-e", printer], env={})
        assert result.returncode == 0, result.stderr
        assert rt in result.stdout, (
            "the managed runtime dirs are not on the child's PATH: a "
            f"provisioned npm shim would not find node.\n{result.stdout}"
        )

    @needs_toolchain
    def test_the_tool_env_travels_with_the_path(self):
        """A relocated git dies without GIT_EXEC_PATH even when it is on PATH.

        This is the failure mode that produced "'remote-http' is not a git
        command" — the binary runs, `git --version` works, and only a clone
        reveals the helpers were never found.
        """
        if registry.tool_path("git") is None:
            pytest.skip("git is not provisioned in this environment")
        printer = "console.log(process.env.GIT_EXEC_PATH || '')"
        result = nodejs.run_node(["-e", printer], env={})
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip(), "GIT_EXEC_PATH was not exported to the child"

    def test_a_missing_tool_raises_rather_than_falling_back(self, tmp_path, monkeypatch):
        """An empty runtime dir is a damaged install, not a fallback signal."""
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path))
        with pytest.raises(nodejs.NotProvisioned) as excinfo:
            nodejs.node_path()
        assert "hermes update" in str(excinfo.value)


class TestNpmInstall:
    @needs_toolchain
    def test_it_installs_from_a_lockfile(self, tmp_path):
        proj = _project(tmp_path, with_lock=True)
        result = nodejs.npm_install(proj)
        assert result.returncode == 0, result.stderr
        assert (proj / "node_modules" / "probe-dep").exists()

    @needs_toolchain
    def test_it_installs_without_a_lockfile(self, tmp_path):
        proj = _project(tmp_path, with_lock=False)
        result = nodejs.npm_install(proj)
        assert result.returncode == 0, result.stderr
        assert (proj / "node_modules" / "probe-dep").exists()

    @needs_toolchain
    def test_it_does_not_rewrite_the_lockfile(self, tmp_path):
        """The contract that PR #65595 was about.

        A mutated lockfile dirties the tree, so the next update stashes it,
        and every later `npm ci` fails against the drifted file.
        """
        proj = _project(tmp_path, with_lock=True)
        before = (proj / "package-lock.json").read_bytes()
        assert nodejs.npm_install(proj).returncode == 0
        assert (proj / "package-lock.json").read_bytes() == before

    @needs_toolchain
    def test_an_out_of_sync_lockfile_still_installs_and_stays_unwritten(self, tmp_path):
        """`npm ci` fails, the fallback runs, and --no-save protects the file."""
        proj = _project(tmp_path, with_lock=True)
        manifest = json.loads((proj / "package.json").read_text(encoding="utf-8"))
        manifest["dependencies"]["probe-dep"] = "file:../dep2"
        (tmp_path / "dep2").mkdir()
        (tmp_path / "dep2" / "package.json").write_text(
            json.dumps({"name": "probe-dep", "version": "2.0.0"}), encoding="utf-8"
        )
        (proj / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        before = (proj / "package-lock.json").read_bytes()

        result = nodejs.npm_install(proj)
        assert result.returncode == 0, result.stderr
        assert (proj / "node_modules" / "probe-dep").exists()
        assert (proj / "package-lock.json").read_bytes() == before

    @needs_toolchain
    def test_dev_dependencies_install_under_node_env_production(self, tmp_path):
        """NODE_ENV=production makes npm omit devDeps silently — exit 0.

        The build then dies later with `tsc: command not found`, far from the
        cause. --include=dev is what prevents it, and only an install run
        under that env proves the flag is doing its job.
        """
        dep = tmp_path / "dep"
        dep.mkdir()
        (dep / "package.json").write_text(
            json.dumps({"name": "probe-dep", "version": "1.0.0"}), encoding="utf-8"
        )
        proj = tmp_path / "withdev"
        proj.mkdir()
        (proj / "package.json").write_text(
            json.dumps(
                {
                    "name": "withdev",
                    "version": "1.0.0",
                    "private": True,
                    "devDependencies": {"probe-dep": "file:../dep"},
                }
            ),
            encoding="utf-8",
        )
        result = nodejs.npm_install(proj, env={"NODE_ENV": "production"})
        assert result.returncode == 0, result.stderr
        assert (proj / "node_modules" / "probe-dep").exists(), (
            "devDependencies were omitted: --include=dev is not reaching npm"
        )


class TestLockfileHashCache:
    def test_a_fresh_project_counts_as_changed(self, tmp_path):
        proj = tmp_path / "p"
        proj.mkdir()
        (proj / "package.json").write_text("{}", encoding="utf-8")
        assert nodejs.lockfile_changed(tmp_path, proj) is True

    def test_recording_then_checking_reports_unchanged(self, tmp_path):
        proj = tmp_path / "p"
        (proj / "node_modules").mkdir(parents=True)
        (proj / "package.json").write_text('{"name":"p"}', encoding="utf-8")
        (proj / "package-lock.json").write_text('{"lockfileVersion":3}', encoding="utf-8")
        nodejs.record_lockfile_hash(tmp_path, proj)
        assert nodejs.lockfile_changed(tmp_path, proj) is False

    def test_editing_a_manifest_reports_changed(self, tmp_path):
        proj = tmp_path / "p"
        (proj / "node_modules").mkdir(parents=True)
        (proj / "package.json").write_text('{"name":"p"}', encoding="utf-8")
        nodejs.record_lockfile_hash(tmp_path, proj)
        (proj / "package.json").write_text('{"name":"p","x":1}', encoding="utf-8")
        assert nodejs.lockfile_changed(tmp_path, proj) is True

    def test_a_matching_hash_with_no_node_modules_reports_changed(self, tmp_path):
        """Another checkout recorded this digest; this tree still needs deps."""
        proj = tmp_path / "p"
        proj.mkdir()
        (proj / "package.json").write_text('{"name":"p"}', encoding="utf-8")
        nodejs.record_lockfile_hash(tmp_path, proj)
        assert nodejs.lockfile_changed(tmp_path, proj) is True

    def test_two_projects_do_not_share_an_answer(self, tmp_path):
        """Parallel worktrees must not skip each other's installs."""
        a, b = tmp_path / "a", tmp_path / "b"
        for p in (a, b):
            (p / "node_modules").mkdir(parents=True)
            (p / "package-lock.json").write_text(
                '{"lockfileVersion":3}', encoding="utf-8"
            )
        (a / "package.json").write_text('{"name":"a"}', encoding="utf-8")
        (b / "package.json").write_text('{"name":"b"}', encoding="utf-8")
        nodejs.record_lockfile_hash(tmp_path, a)
        assert nodejs.lockfile_changed(tmp_path, a) is False
        assert nodejs.lockfile_changed(tmp_path, b) is True

    def test_a_workspace_manifest_edit_counts_as_changed(self, tmp_path):
        """One lockfile spans the whole graph, so any member edit matters.

        A developer can edit a workspace package.json without running npm:
        the lockfile is untouched, but the tree needs syncing. Hashing only
        the root pair would skip that install and leave node_modules stale.
        """
        proj = tmp_path / "p"
        (proj / "node_modules").mkdir(parents=True)
        (proj / "package-lock.json").write_text(
            '{"lockfileVersion":3}', encoding="utf-8"
        )
        (proj / "package.json").write_text(
            '{"name":"p","workspaces":["packages/*"]}', encoding="utf-8"
        )
        member = proj / "packages" / "one"
        member.mkdir(parents=True)
        (member / "package.json").write_text('{"name":"one"}', encoding="utf-8")

        nodejs.record_lockfile_hash(tmp_path, proj)
        assert nodejs.lockfile_changed(tmp_path, proj) is False

        (member / "package.json").write_text(
            '{"name":"one","dependencies":{"left-pad":"^1.0.0"}}', encoding="utf-8"
        )
        assert nodejs.lockfile_changed(tmp_path, proj) is True

    def test_a_lockfile_is_required_for_a_digest(self, tmp_path):
        """No lockfile means no basis for comparison, so never skip."""
        proj = tmp_path / "p"
        proj.mkdir()
        (proj / "package.json").write_text('{"name":"p"}', encoding="utf-8")
        assert nodejs.manifests_digest(proj) is None
        assert nodejs.lockfile_changed(tmp_path, proj) is True

    def test_a_project_with_no_manifests_has_no_digest(self, tmp_path):
        proj = tmp_path / "empty"
        proj.mkdir()
        assert nodejs.manifests_digest(proj) is None
        assert nodejs.lockfile_changed(tmp_path, proj) is True

    def test_an_unwritable_state_dir_is_not_fatal(self, tmp_path):
        """A cache we cannot write means the next run reinstalls. Slow, correct."""
        proj = tmp_path / "p"
        (proj / "node_modules").mkdir(parents=True)
        (proj / "package.json").write_text('{"name":"p"}', encoding="utf-8")
        state = tmp_path / "state"
        state.mkdir()
        state.chmod(0o500)
        try:
            nodejs.record_lockfile_hash(state, proj)  # must not raise
            assert nodejs.lockfile_changed(state, proj) is True
        finally:
            state.chmod(0o700)
