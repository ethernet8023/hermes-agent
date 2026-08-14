"""Tests for installation.provisioner.

The decision core runs against a LOCAL http server serving real archives,
so download → verify → extract → run → record is exercised end to end
without reaching the network. What is asserted is the contract: exact
digests gate everything, a tool that cannot be verified is not recorded,
one tool's failure does not stop the others, and nothing is ever adopted
from disk without verification (there is no salvage).
"""

import hashlib
import json
import tarfile
import threading
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from installation import provisioner as rp
from installation import registry as rr
from hermes_constants import get_runtime_dir


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """Serve a directory of fixture archives over http://127.0.0.1."""
    root = tmp_path_factory.mktemp("served")
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield root, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _script(text: str = "#!/bin/sh\necho 'tool 1.2.3'\n") -> bytes:
    return text.encode()


def _make_tar(root: Path, name: str, members: dict[str, bytes]) -> str:
    """Write a .tar.gz of {relative path: bytes}, all executable."""
    staging = root / f".stage-{name}"
    staging.mkdir(parents=True, exist_ok=True)
    for rel, data in members.items():
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755)

    archive = root / name
    with tarfile.open(archive, "w:gz") as tf:
        for rel in members:
            tf.add(staging / rel, arcname=rel)
    return hashlib.sha256(archive.read_bytes()).hexdigest()


def _pins_file(root: Path, tools: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / rr.PINS_FILENAME).write_text(
        json.dumps({"schemaVersion": rr.PINS_SCHEMA_VERSION, "tools": tools}),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def target():
    """Provision for THIS host so the run-the-binary check is exercised."""
    return rr.current_target()


class TestProvisionOneTool:
    def test_downloads_verifies_runs_and_records(self, served, tmp_path, target):
        root, base = served
        sha = _make_tar(root, "gh-ok.tar.gz", {"bin/gh": _script()})
        pins = _pins_file(
            tmp_path / "repo",
            {
                "gh": {
                    "version": "2.97.0",
                    "files": {target: {"url": f"{base}/gh-ok.tar.gz", "sha256": sha}},
                }
            },
        )

        rt = tmp_path / "rt"
        results = rp.provision_runtimes(runtime_dir=rt, install_root=pins)

        assert [(r.tool, r.action, r.version) for r in results] == [
            ("gh", "downloaded", "2.97.0")
        ]
        # runtime_dir with no store means the runtime dir IS the store, so
        # the entry lands beside the facts file that names it.
        assert (rt / f"gh-2.97.0-{target}" / "bin" / "gh").is_file()
        assert rr.load_facts(rt)["gh"].path == f"gh-2.97.0-{target}/bin/gh"

    def test_second_run_keeps_an_exact_match(self, served, tmp_path, target):
        """Exact pins make this an equality check, not a range check."""
        root, base = served
        sha = _make_tar(root, "gh-keep.tar.gz", {"bin/gh": _script()})
        pins = _pins_file(
            tmp_path / "repo",
            {
                "gh": {
                    "version": "2.97.0",
                    "files": {target: {"url": f"{base}/gh-keep.tar.gz", "sha256": sha}},
                }
            },
        )
        rt = tmp_path / "rt"

        rp.provision_runtimes(runtime_dir=rt, install_root=pins)
        again = rp.provision_runtimes(runtime_dir=rt, install_root=pins)

        assert [r.action for r in again] == ["kept"]

    def test_a_version_bump_reprovisions(self, served, tmp_path, target):
        root, base = served
        sha_old = _make_tar(root, "gh-old.tar.gz", {"bin/gh": _script()})
        sha_new = _make_tar(root, "gh-new.tar.gz", {"bin/gh": _script()})
        repo = tmp_path / "repo"
        rt = tmp_path / "rt"

        _pins_file(repo, {"gh": {"version": "2.97.0", "files": {
            target: {"url": f"{base}/gh-old.tar.gz", "sha256": sha_old}}}})
        rp.provision_runtimes(runtime_dir=rt, install_root=repo)

        _pins_file(repo, {"gh": {"version": "2.98.0", "files": {
            target: {"url": f"{base}/gh-new.tar.gz", "sha256": sha_new}}}})
        results = rp.provision_runtimes(runtime_dir=rt, install_root=repo)

        assert [(r.action, r.version) for r in results] == [("downloaded", "2.98.0")]
        assert rr.load_facts(rt)["gh"].version == "2.98.0"

    def test_nested_archives_are_flattened(self, served, tmp_path, target):
        """node/gh/ripgrep nest under a versioned dir; uv nests on POSIX
        and not on Windows. Flattening keys off the archive's shape, so
        the facts path stays stable either way."""
        root, base = served
        sha = _make_tar(root, "gh-nested.tar.gz", {"gh_2.97.0_linux/bin/gh": _script()})
        pins = _pins_file(tmp_path / "repo", {"gh": {"version": "2.97.0", "files": {
            target: {"url": f"{base}/gh-nested.tar.gz", "sha256": sha}}}})

        rt = tmp_path / "rt"
        rp.provision_runtimes(runtime_dir=rt, install_root=pins)

        assert (rt / f"gh-2.97.0-{target}" / "bin" / "gh").is_file()


class TestDigestIsTheGate:
    def test_a_mismatched_digest_aborts_before_extracting(
        self, served, tmp_path, target
    ):
        root, base = served
        _make_tar(root, "gh-tampered.tar.gz", {"bin/gh": _script()})
        pins = _pins_file(tmp_path / "repo", {"gh": {"version": "2.97.0", "files": {
            target: {"url": f"{base}/gh-tampered.tar.gz", "sha256": "e" * 64}}}})

        rt = tmp_path / "rt"
        results = rp.provision_runtimes(runtime_dir=rt, install_root=pins)

        assert results[0].action == "failed"
        assert "sha256 mismatch" in (results[0].detail or "")
        # Nothing unpacked, nothing recorded: the bytes were never trusted.
        assert not (rt / "gh").exists()
        assert rr.load_facts(rt) == {}

    def test_a_missing_download_fails_that_tool_only(self, served, tmp_path, target):
        root, base = served
        sha = _make_tar(root, "ok.tar.gz", {"bin/gh": _script()})
        pins = _pins_file(tmp_path / "repo", {
            "gh": {"version": "1.0.0", "files": {
                target: {"url": f"{base}/ok.tar.gz", "sha256": sha}}},
            "ripgrep": {"version": "1.0.0", "files": {
                target: {"url": f"{base}/absent.tar.gz", "sha256": "b" * 64}}},
        })

        rt = tmp_path / "rt"
        results = {r.tool: r.action for r in
                   rp.provision_runtimes(runtime_dir=rt, install_root=pins)}

        # A broken ripgrep download must not stop gh from provisioning.
        assert results == {"gh": "downloaded", "ripgrep": "failed"}
        assert "gh" in rr.load_facts(rt)
        assert "ripgrep" not in rr.load_facts(rt)


class TestScratchCleanupIsNotAFailure:
    """The scratch dir is a convenience, never a gate.

    On Windows the downloaded artifact is routinely still held open when
    cleanup runs: the PortableGit self-extractor outlives its own exit,
    and Defender cannot be disabled on the windows-11-arm image, so it
    scans the .exe. The delete then fails with WinError 5 AFTER the tool
    is already staged and verified. These tests drive the real cleanup
    with a real undeletable file (a read-only parent dir) instead of
    faking the host.
    """

    def test_discarding_an_undeletable_scratch_dir_does_not_raise(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        (scratch / "held-open.exe").write_bytes(b"still open elsewhere")
        scratch.chmod(0o500)  # unlinking a child now raises PermissionError
        try:
            rp._discard_scratch(scratch)
        finally:
            scratch.chmod(0o700)

    def test_an_undeletable_scratch_file_still_provisions(
        self, served, tmp_path, target, monkeypatch
    ):
        root, base = served
        sha = _make_tar(root, "gh-locked.tar.gz", {"bin/gh": _script()})
        pins = _pins_file(tmp_path / "repo", {"gh": {"version": "2.97.0", "files": {
            target: {"url": f"{base}/gh-locked.tar.gz", "sha256": sha}}}})

        real_stage = rp._stage
        locked: list[Path] = []

        def stage_then_lock(tool, pin, dest, tmp, tgt, rt):
            real_stage(tool, pin, dest, tmp, tgt, rt)
            (tmp / "held-open.exe").write_bytes(b"still open elsewhere")
            tmp.chmod(0o500)
            locked.append(tmp)

        monkeypatch.setattr(rp, "_stage", stage_then_lock)

        rt = tmp_path / "rt"
        try:
            results = rp.provision_runtimes(runtime_dir=rt, install_root=pins)
        finally:
            for tmp in locked:
                tmp.chmod(0o700)

        assert [(r.tool, r.action) for r in results] == [("gh", "downloaded")]
        assert (rt / f"gh-2.97.0-{target}" / "bin" / "gh").is_file()
        assert rr.load_facts(rt)["gh"].version == "2.97.0"


class TestVerificationBeforeRecording:
    def test_an_unrunnable_binary_is_not_recorded(self, served, tmp_path, target):
        """Recording it would tell every reader a broken tool is ready."""
        root, base = served
        sha = _make_tar(root, "gh-broken.tar.gz", {"bin/gh": b"\x00\x01not a program"})
        pins = _pins_file(tmp_path / "repo", {"gh": {"version": "2.97.0", "files": {
            target: {"url": f"{base}/gh-broken.tar.gz", "sha256": sha}}}})

        rt = tmp_path / "rt"
        results = rp.provision_runtimes(runtime_dir=rt, install_root=pins)

        assert results[0].action == "failed"
        assert "does not run" in (results[0].detail or "")
        assert rr.load_facts(rt) == {}

    def test_an_archive_without_the_expected_binary_fails(
        self, served, tmp_path, target
    ):
        root, base = served
        sha = _make_tar(root, "gh-empty.tar.gz", {"README": b"nothing here"})
        pins = _pins_file(tmp_path / "repo", {"gh": {"version": "2.97.0", "files": {
            target: {"url": f"{base}/gh-empty.tar.gz", "sha256": sha}}}})

        rt = tmp_path / "rt"
        results = rp.provision_runtimes(runtime_dir=rt, install_root=pins)

        assert results[0].action == "failed"
        assert "missing after staging" in (results[0].detail or "")


class TestNoSalvage:
    def test_an_unverified_tree_on_disk_is_replaced_not_adopted(
        self, served, tmp_path, target
    ):
        """There is no salvage: adopting bytes nobody verified would
        defeat pinning digests at all.

        A squatter at the entry path with no published marker is not an
        entry — it is junk from an interrupted run — so the provisioner
        overwrites it rather than trusting it.
        """
        root, base = served
        sha = _make_tar(root, "gh-fresh.tar.gz", {"bin/gh": _script()})
        pins = _pins_file(tmp_path / "repo", {"gh": {"version": "2.97.0", "files": {
            target: {"url": f"{base}/gh-fresh.tar.gz", "sha256": sha}}}})

        rt = tmp_path / "rt"
        squatter = rt / f"gh-2.97.0-{target}" / "bin" / "gh"
        squatter.parent.mkdir(parents=True)
        squatter.write_text("#!/bin/sh\necho 'impostor 9.9.9'\n")
        squatter.chmod(0o755)

        results = rp.provision_runtimes(runtime_dir=rt, install_root=pins)

        assert results[0].action == "downloaded"
        assert "impostor" not in squatter.read_text()

    def test_the_provisioner_exposes_no_salvage_surface(self):
        for name in dir(rp):
            assert "salvage" not in name.lower()


class TestTheStoreIsShared:
    """Bytes are machine-wide; facts are per-install. That is the whole
    reason the store exists — 44 worktrees cost 44 JSON files and ONE
    copy of node, not 44 copies at ~495MB each."""

    def _gh_pins(self, served, tmp_path, target, name="gh-shared.tar.gz"):
        root, base = served
        sha = _make_tar(root, name, {"bin/gh": _script()})
        return _pins_file(tmp_path / f"repo-{name}", {"gh": {
            "version": "2.97.0",
            "files": {target: {"url": f"{base}/{name}", "sha256": sha}}}})

    def test_a_second_install_reuses_the_bytes_without_downloading(
        self, served, tmp_path, target, monkeypatch
    ):
        """The sharing, proven by cutting the network after install one."""
        pins = self._gh_pins(served, tmp_path, target)
        store = tmp_path / "store"

        first = rp.provision_runtimes(
            runtime_dir=tmp_path / "install-a", install_root=pins, store_dir=store
        )
        assert [r.action for r in first] == ["downloaded"]

        def no_downloads(*args, **kwargs):
            raise AssertionError("a shared store entry must not be re-downloaded")

        monkeypatch.setattr(rp, "_download", no_downloads)

        second = rp.provision_runtimes(
            runtime_dir=tmp_path / "install-b", install_root=pins, store_dir=store
        )

        assert [r.action for r in second] == ["adopted"]  # fact written, no fetch
        # One copy of the bytes, two facts files naming it.
        assert len(list(store.glob("gh-*"))) == 1
        for install in ("install-a", "install-b"):
            facts = rr.load_facts(tmp_path / install)
            assert facts["gh"].path == f"gh-2.97.0-{target}/bin/gh"
            assert (store / facts["gh"].path).is_file()

    def test_an_installer_published_entry_is_adopted_not_refetched(
        self, served, tmp_path, target, monkeypatch
    ):
        """The install.sh/install.ps1 handshake, frozen.

        The installers stage the bootstrap git into the store BEFORE the
        repo exists, speaking the store's protocol by hand: entry named
        <tool>-<version>-<target>, marker file inside, staged-then-moved.
        The provisioner must treat such an entry exactly like one of its
        own — adopt it, write the fact, download nothing — or every fresh
        install fetches its first tool twice.
        """
        pins = self._gh_pins(served, tmp_path, target, name="gh-boot.tar.gz")
        store = tmp_path / "store"

        # Publish the way the installers do: bytes + marker, no facts.
        entry = store / f"gh-2.97.0-{target}"
        (entry / "bin").mkdir(parents=True)
        (entry / "bin" / "gh").write_bytes(_script())
        (entry / "bin" / "gh").chmod(0o755)
        (entry / rp.ENTRY_MARKER_NAME).write_text(
            json.dumps(
                {
                    "tool": "gh",
                    "version": "2.97.0",
                    "target": target,
                    "sha256": "0" * 64,  # installers record the archive digest
                    "publishedAt": "2026-08-14T00:00:00+00:00",
                }
            )
        )

        def no_downloads(*args, **kwargs):
            raise AssertionError("an installer-published entry must be adopted")

        monkeypatch.setattr(rp, "_download", no_downloads)

        results = rp.provision_runtimes(
            runtime_dir=tmp_path / "install", install_root=pins, store_dir=store
        )

        assert [(r.action, r.version) for r in results] == [("adopted", "2.97.0")]
        facts = rr.load_facts(tmp_path / "install")
        assert facts["gh"].path == f"gh-2.97.0-{target}/bin/gh"

    def test_facts_stay_per_install(self, served, tmp_path, target):
        """One install recording a tool must not make it appear in
        another install that never provisioned it."""
        pins = self._gh_pins(served, tmp_path, target, "gh-facts.tar.gz")
        store = tmp_path / "store"

        rp.provision_runtimes(
            runtime_dir=tmp_path / "install-a", install_root=pins, store_dir=store
        )

        assert rr.load_facts(tmp_path / "install-b") == {}

    def test_a_version_bump_leaves_the_old_entry_alone(
        self, served, tmp_path, target
    ):
        """Immutability: another install may be RUNNING the old entry, so
        the bump writes a new one and repoints only this install's fact."""
        root, base = served
        sha_old = _make_tar(root, "gh-v1.tar.gz", {"bin/gh": _script()})
        sha_new = _make_tar(root, "gh-v2.tar.gz", {"bin/gh": _script()})
        repo = tmp_path / "repo"
        store = tmp_path / "store"
        rt = tmp_path / "rt"

        _pins_file(repo, {"gh": {"version": "2.97.0", "files": {
            target: {"url": f"{base}/gh-v1.tar.gz", "sha256": sha_old}}}})
        rp.provision_runtimes(runtime_dir=rt, install_root=repo, store_dir=store)
        old_entry = store / f"gh-2.97.0-{target}"
        old_stat = (old_entry / "bin" / "gh").stat()

        _pins_file(repo, {"gh": {"version": "2.98.0", "files": {
            target: {"url": f"{base}/gh-v2.tar.gz", "sha256": sha_new}}}})
        rp.provision_runtimes(runtime_dir=rt, install_root=repo, store_dir=store)

        assert (store / f"gh-2.98.0-{target}" / "bin" / "gh").is_file()
        assert (old_entry / "bin" / "gh").is_file(), "the old entry was destroyed"
        assert (old_entry / "bin" / "gh").stat().st_mtime == old_stat.st_mtime
        assert rr.load_facts(rt)["gh"].path == f"gh-2.98.0-{target}/bin/gh"

    def test_an_entry_published_mid_stage_is_not_overwritten(
        self, served, tmp_path, target, monkeypatch
    ):
        """The publish race, driven for real: another process finishes
        first while we are still extracting. Its entry carries the
        published marker, so ours is dropped and its bytes are untouched."""
        pins = self._gh_pins(served, tmp_path, target, "gh-race.tar.gz")
        store = tmp_path / "store"
        entry = store / f"gh-2.97.0-{target}"

        real_stage = rp._stage

        def stage_then_race(tool, pin, dest, tmp, tgt, ctx):
            real_stage(tool, pin, dest, tmp, tgt, ctx)
            # The "other process" publishes a complete entry — marker and
            # all — while we still hold a staged tree.
            (entry / "bin").mkdir(parents=True)
            (entry / "bin" / "gh").write_text("#!/bin/sh\necho 'winner 2.97.0'\n")
            (entry / "bin" / "gh").chmod(0o755)
            (entry / rp.ENTRY_MARKER_NAME).write_text(
                json.dumps(rp._entry_marker(pin, tool, tgt))
            )

        monkeypatch.setattr(rp, "_stage", stage_then_race)

        results = rp.provision_runtimes(
            runtime_dir=tmp_path / "rt", install_root=pins, store_dir=store
        )

        assert [r.action for r in results] == ["downloaded"]
        assert "winner" in (entry / "bin" / "gh").read_text()
        # And no staging directory is left behind.
        assert list(store.glob(".staging-*")) == []

    def test_an_unmarked_directory_at_an_entry_name_is_replaced(
        self, served, tmp_path, target
    ):
        """No-salvage, in the store: only an entry THIS code published is
        trusted. Junk from an interrupted run has no marker, so nothing
        can be relying on it and it is overwritten rather than adopted."""
        pins = self._gh_pins(served, tmp_path, target, "gh-junk.tar.gz")
        store = tmp_path / "store"
        junk = store / f"gh-2.97.0-{target}" / "bin" / "gh"
        junk.parent.mkdir(parents=True)
        junk.write_text("#!/bin/sh\necho 'impostor 9.9.9'\n")
        junk.chmod(0o755)

        results = rp.provision_runtimes(
            runtime_dir=tmp_path / "rt", install_root=pins, store_dir=store
        )

        assert results[0].action == "downloaded"
        assert "impostor" not in junk.read_text()
        assert (junk.parent.parent / rp.ENTRY_MARKER_NAME).is_file()

    def test_a_published_entry_is_reused_even_with_no_fact(
        self, served, tmp_path, target, monkeypatch
    ):
        """A fresh install landing on a store another install filled: the
        marker is what lets it record the fact without downloading."""
        pins = self._gh_pins(served, tmp_path, target, "gh-nofact.tar.gz")
        store = tmp_path / "store"

        rp.provision_runtimes(
            runtime_dir=tmp_path / "install-a", install_root=pins, store_dir=store
        )

        def no_downloads(*args, **kwargs):
            raise AssertionError("a published entry must not be re-downloaded")

        monkeypatch.setattr(rp, "_download", no_downloads)

        results = rp.provision_runtimes(
            runtime_dir=tmp_path / "install-b", install_root=pins, store_dir=store
        )

        assert [r.action for r in results] == ["adopted"]
        assert rr.load_facts(tmp_path / "install-b")["gh"].version == "2.97.0"

    def test_staging_dirs_do_not_survive_a_successful_publish(
        self, served, tmp_path, target
    ):
        pins = self._gh_pins(served, tmp_path, target, "gh-clean.tar.gz")
        store = tmp_path / "store"

        rp.provision_runtimes(
            runtime_dir=tmp_path / "rt", install_root=pins, store_dir=store
        )

        assert list(store.glob(".staging-*")) == []


class TestSelectiveProvisioning:
    def test_only_provisions_the_named_tool(self, served, tmp_path, target):
        root, base = served
        sha = _make_tar(root, "sel.tar.gz", {"bin/gh": _script()})
        pins = _pins_file(tmp_path / "repo", {
            "gh": {"version": "1.0.0", "files": {
                target: {"url": f"{base}/sel.tar.gz", "sha256": sha}}},
            "ripgrep": {"version": "1.0.0", "files": {
                target: {"url": f"{base}/absent.tar.gz", "sha256": "c" * 64}}},
        })

        results = rp.provision_runtimes(
            runtime_dir=tmp_path / "rt", install_root=pins, only=["gh"]
        )

        assert [r.tool for r in results] == ["gh"]


class TestSealedInstallStalenessGate:
    """A sealed tree cannot provision, so drift there is fatal.

    A git checkout heals itself on the next update — raising would break
    the very run that fixes it. A Nix/Docker/desktop artifact has its
    tools built in by its steward, so a mismatch means the artifact was
    assembled against a different pin table than the code it ships.
    """

    def _sealed(self, root: Path, steward: str = "nix") -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "install-stamp.json").write_text(
            json.dumps({"distribution": steward}), encoding="utf-8"
        )
        return root

    def _current_runtime(self, rt: Path, pins_root: Path, target: str) -> None:
        """A runtime dir that satisfies every pin in *pins_root*."""
        facts = {}
        for tool, entry in rr.load_pins(pins_root).items():
            rel = rp._binary_rel(tool, target)
            binary = rt / rel
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text("#!/bin/sh\n")
            facts[tool] = rr.RuntimeFact(version=entry["version"], path=rel)
        rr.save_facts(facts, rt)

    def test_a_current_sealed_install_passes(self, tmp_path, target):
        pins = _pins_file(tmp_path / "repo", {
            "gh": {"version": "2.97.0", "files": {
                target: {"url": "https://example.invalid/gh.tar.gz", "sha256": "a" * 64}}},
        })
        rt = tmp_path / "rt"
        self._current_runtime(rt, pins, target)

        rp.require_current_runtimes(
            project_root=self._sealed(tmp_path / "sealed"),
            runtime_dir=rt,
            install_root=pins,
        )

    def test_a_stale_sealed_install_refuses_with_the_drift_named(
        self, tmp_path, target
    ):
        pins = _pins_file(tmp_path / "repo", {
            "gh": {"version": "2.98.0", "files": {
                target: {"url": "https://example.invalid/gh.tar.gz", "sha256": "a" * 64}}},
        })
        rt = tmp_path / "rt"
        rel = rp._binary_rel("gh", target)
        binary = rt / rel
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n")
        rr.save_facts({"gh": rr.RuntimeFact(version="2.97.0", path=rel)}, rt)

        with pytest.raises(rp.StaleManagedRuntimes) as excinfo:
            rp.require_current_runtimes(
                project_root=self._sealed(tmp_path / "sealed"),
                runtime_dir=rt,
                install_root=pins,
            )

        message = str(excinfo.value)
        assert "nix" in message
        # Naming the versions is the point: "rebuild it" is unactionable
        # without knowing what drifted.
        assert "2.98.0" in message and "2.97.0" in message

    def test_a_git_checkout_with_the_same_drift_does_not_raise(
        self, tmp_path, target
    ):
        """It provisions on demand; the next update fixes it."""
        pins = _pins_file(tmp_path / "repo", {
            "gh": {"version": "2.98.0", "files": {
                target: {"url": "https://example.invalid/gh.tar.gz", "sha256": "a" * 64}}},
        })
        checkout = tmp_path / "checkout"
        (checkout / ".git").mkdir(parents=True)

        rp.require_current_runtimes(
            project_root=checkout, runtime_dir=tmp_path / "empty", install_root=pins
        )

    def test_an_unprovisioned_sealed_install_refuses(self, tmp_path, target):
        """Nothing installed at all is drift too — a sealed artifact is
        supposed to ship its tools already built."""
        pins = _pins_file(tmp_path / "repo", {
            "gh": {"version": "2.97.0", "files": {
                target: {"url": "https://example.invalid/gh.tar.gz", "sha256": "a" * 64}}},
        })

        with pytest.raises(rp.StaleManagedRuntimes, match="nothing"):
            rp.require_current_runtimes(
                project_root=self._sealed(tmp_path / "sealed"),
                runtime_dir=tmp_path / "empty",
                install_root=pins,
            )

    def test_a_recorded_but_vanished_binary_counts_as_stale(self, tmp_path, target):
        """Every other reader treats recorded-but-missing as
        unprovisioned; the gate must not call it current."""
        pins = _pins_file(tmp_path / "repo", {
            "gh": {"version": "2.97.0", "files": {
                target: {"url": "https://example.invalid/gh.tar.gz", "sha256": "a" * 64}}},
        })
        rt = tmp_path / "rt"
        # A fact, but no file behind it.
        rr.save_facts(
            {"gh": rr.RuntimeFact(version="2.97.0", path=rp._binary_rel("gh", target))},
            rt,
        )

        assert rp.stale_tools(runtime_dir=rt, install_root=pins) == {
            "gh": ("2.97.0", None)
        }


class TestPackagedInstallsLocateTheirPins:
    """A sealed venv has no repo root above site-packages.

    The pin table is not a Python package and is deliberately NOT shipped
    as wheel package-data — we build wheels only for the Nix package, and
    package-data would put the table in every wheel anyone ever builds.
    The packager points at it instead, the same way it already points at
    optional-skills, locales and the build stamp.
    """

    def test_the_override_locates_a_table_outside_the_source_tree(
        self, tmp_path, monkeypatch
    ):
        table = tmp_path / "store" / rr.PINS_FILENAME
        table.parent.mkdir(parents=True)
        table.write_text(
            json.dumps({
                "schemaVersion": rr.PINS_SCHEMA_VERSION,
                "tools": {"gh": {"version": "9.9.9", "files": {
                    "any": {"url": "https://example.invalid/gh.tgz",
                            "sha256": "a" * 64}}}},
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_RUNTIME_PINS", str(table))

        assert rr.pins_path() == table
        assert rr.load_pins()["gh"]["version"] == "9.9.9"

    def test_an_explicit_install_root_still_wins(self, tmp_path, monkeypatch):
        """A caller naming a root means that root — the override is for
        installs that have no repo root at all, not a global redirect."""
        monkeypatch.setenv("HERMES_RUNTIME_PINS", str(tmp_path / "ignored.json"))

        assert rr.pins_path(tmp_path / "explicit") == (
            tmp_path / "explicit" / rr.PINS_FILENAME
        )

    def test_without_the_override_the_repo_table_is_used(self, monkeypatch):
        monkeypatch.delenv("HERMES_RUNTIME_PINS", raising=False)

        # The checkout's own table, beside the package — unchanged
        # behaviour for every non-packaged install.
        assert rr.pins_path().name == rr.PINS_FILENAME
        assert rr.load_pins()["node"]["version"]


class TestPackagedInstallsLocateTheirRuntimeDir:
    def test_the_override_points_at_a_prebuilt_runtime_dir(
        self, tmp_path, monkeypatch
    ):
        """Nix BUILDS the runtime dir instead of provisioning one: its
        install root is an immutable store path nothing can write to."""
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "prebuilt"))

        assert get_runtime_dir() == tmp_path / "prebuilt"

    def test_an_explicit_install_root_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "ignored"))

        resolved = get_runtime_dir(install_root=tmp_path / "explicit")

        assert resolved.parent == tmp_path / "explicit"


class TestLayout:
    def test_windows_and_posix_binaries_land_where_readers_expect(self):
        """Inside a tool's own store entry — the entry dir already carries
        the tool name, version and target."""
        assert rp._binary_rel("node", "win32-x64") == "node.exe"
        assert rp._binary_rel("node", "linux-x64") == "bin/node"
        # Two git suppliers, two layouts.
        assert rp._binary_rel("git", "win32-x64") == "cmd/git.exe"
        assert rp._binary_rel("git", "darwin-arm64") == "bin/git"
        # npm is installed by npm, which writes .cmd shims in the prefix
        # root on Windows and POSIX shims in bin/.
        assert rp._binary_rel("npm", "win32-x64") == "npm.cmd"
        assert rp._binary_rel("npm", "darwin-arm64") == "bin/npm"

    def test_a_recorded_path_names_the_store_entry(self):
        """What lands in runtimes.json is store-relative, so a reader
        joins it onto the store and lands in the right VERSION's entry."""
        assert (
            rp._fact_rel("node", "26.7.0", "linux-x64") == "node-26.7.0-linux-x64/bin/node"
        )
        assert (
            rp._fact_rel("git", "2.53.0.3", "win32-x64")
            == "git-2.53.0.3-win32-x64/cmd/git.exe"
        )

    def test_two_versions_of_one_tool_are_different_entries(self):
        """The whole point of the store: a branch pinning an older node
        does not overwrite the newer one another install is running."""
        old = rp._fact_rel("node", "22.1.0", "linux-x64")
        new = rp._fact_rel("node", "26.7.0", "linux-x64")

        assert old != new
        assert old.split("/")[0] != new.split("/")[0]

    def test_only_portablegit_needs_extra_path_dirs(self):
        """bash.exe and the coreutils live outside cmd/; every other tool
        is covered by its binary's own directory."""
        assert rp._path_dirs("git", "win32-x64") == ["cmd", "bin", "usr/bin"]
        assert rp._path_dirs("git", "darwin-arm64") is None
        assert rp._path_dirs("node", "win32-x64") is None

    def test_recorded_path_dirs_are_store_relative_too(self):
        """A PATH dir must resolve against the same base as the binary,
        or the assembler emits three dirs that do not exist."""
        assert rp._fact_path_dirs("git", "2.53.0.3", "win32-x64") == [
            "git-2.53.0.3-win32-x64/cmd",
            "git-2.53.0.3-win32-x64/bin",
            "git-2.53.0.3-win32-x64/usr/bin",
        ]
        assert rp._fact_path_dirs("node", "26.7.0", "win32-x64") is None


class TestExtendsOrdering:
    """`extends` in the pin table drives provisioning order and the
    recorded PATH order — the provisioner never restates either.

    These use gh/ripgrep rather than the real npm/node pair: the edge is
    generic machinery, and naming npm would drag in its bespoke staging
    (which needs a real node) and test two things at once.
    """

    def test_a_tool_is_provisioned_after_what_it_extends(self, served, tmp_path, target):
        """Declared in the wrong order on purpose: the edge decides, not
        the order someone happened to type the entries in."""
        root, base = served
        sha = _make_tar(root, "ordering.tar.gz", {"bin/gh": _script()})
        rg_sha = _make_tar(root, "ordering-rg.tar.gz", {"rg": _script()})
        pins = _pins_file(tmp_path / "repo", {
            "gh": {"version": "1.0.0", "extends": ["ripgrep"], "files": {
                target: {"url": f"{base}/ordering.tar.gz", "sha256": sha}}},
            "ripgrep": {"version": "1.0.0", "files": {
                target: {"url": f"{base}/ordering-rg.tar.gz", "sha256": rg_sha}}},
        })

        results = rp.provision_runtimes(runtime_dir=tmp_path / "rt", install_root=pins)

        assert [r.tool for r in results] == ["ripgrep", "gh"]

    def test_the_recorded_path_order_puts_the_extender_first(
        self, served, tmp_path, target
    ):
        """An extender has to be FOUND before what it extends (npm before
        node, or node's bundled npm wins); readers get that from the
        facts file, not from a list of their own."""
        root, base = served
        sha = _make_tar(root, "recorded.tar.gz", {"bin/gh": _script()})
        rg_sha = _make_tar(root, "recorded-rg.tar.gz", {"rg": _script()})
        pins = _pins_file(tmp_path / "repo", {
            "ripgrep": {"version": "1.0.0", "files": {
                target: {"url": f"{base}/recorded-rg.tar.gz", "sha256": rg_sha}}},
            "gh": {"version": "1.0.0", "extends": ["ripgrep"], "files": {
                target: {"url": f"{base}/recorded.tar.gz", "sha256": sha}}},
        })

        rt = tmp_path / "rt"
        rp.provision_runtimes(runtime_dir=rt, install_root=pins)

        order = rr.load_path_order(rt)
        assert order.index("gh") < order.index("ripgrep")

    def test_an_extender_fails_cleanly_when_what_it_extends_is_absent(
        self, served, tmp_path, target
    ):
        """node failing must not produce a half-installed npm recorded as
        ready — the reader would then put a broken shim on PATH."""
        root, base = served
        pins = _pins_file(tmp_path / "repo", {
            "node": {"version": "1.0.0", "files": {
                target: {"url": f"{base}/absent.tar.gz", "sha256": "d" * 64}}},
            "npm": {"version": "1.0.0", "extends": ["node"], "files": {
                target: {"url": f"{base}/absent.tar.gz", "sha256": "e" * 64}}},
        })

        rt = tmp_path / "rt"
        results = {r.tool: r.action for r in
                   rp.provision_runtimes(runtime_dir=rt, install_root=pins)}

        assert results == {"node": "failed", "npm": "failed"}
        assert rr.load_facts(rt) == {}


class TestTargetIsAnAssertionNotAChoice:
    """``--target`` states the host; it never selects a different one.

    Provisioning stages binaries and then RUNS them to record a fact. A
    target other than this host could not be probed here, so the flag
    refuses instead of writing facts nobody verified.
    """

    def test_a_matching_target_is_accepted(self, tmp_path, monkeypatch):
        rt = tmp_path / "rt"
        # No pins to fetch: this asserts the gate lets the host through,
        # not that provisioning succeeds.
        monkeypatch.setattr(rp, "load_pins", lambda install_root=None: {})
        code = rp.main(["--runtime-dir", str(rt), "--target", rr.current_target()])
        assert code == 0

    def test_a_mismatched_target_exits_2(self, tmp_path, capsys):
        rt = tmp_path / "rt"
        wrong = "sunos-vax" if rr.current_target() != "sunos-vax" else "linux-x64"
        code = rp.main(["--runtime-dir", str(rt), "--target", wrong])
        assert code == 2
        assert wrong in capsys.readouterr().err

    def test_the_gate_runs_before_any_download(self, tmp_path, monkeypatch):
        """A refused target must not touch the network or the disk."""
        called = []
        monkeypatch.setattr(rp, "load_pins", lambda install_root=None: called.append("pins") or {})
        rt = tmp_path / "rt"
        assert rp.main(["--runtime-dir", str(rt), "--target", "sunos-vax"]) == 2
        assert called == []
        assert not rt.exists()


class TestOptionalTools:
    """An optional pin backs a capability the user may never touch.

    The sweep must not download it, ``provision_tool`` must, and once it
    is recorded the sweep owns it like any other tool — that last part is
    what carries a pin bump onto an install that uses the capability.
    """

    @staticmethod
    def _pins(root: Path, base: str, sha: str, target: str, opt_sha: str) -> Path:
        return _pins_file(
            root,
            {
                "gh": {
                    "version": "2.97.0",
                    "files": {target: {"url": f"{base}/opt-gh.tar.gz", "sha256": sha}},
                },
                "ripgrep": {
                    "version": "1.0.0",
                    "optional": True,
                    "files": {
                        target: {"url": f"{base}/opt-ripgrep.tar.gz", "sha256": opt_sha}
                    },
                },
            },
        )

    def test_sweep_skips_an_optional_tool_nobody_asked_for(
        self, served, tmp_path, target
    ):
        root, base = served
        sha = _make_tar(root, "opt-gh.tar.gz", {"bin/gh": _script()})
        opt_sha = _make_tar(root, "opt-ripgrep.tar.gz", {"rg": _script()})
        pins = self._pins(tmp_path / "repo", base, sha, target, opt_sha)
        rt = tmp_path / "rt"

        results = rp.provision_runtimes(runtime_dir=rt, install_root=pins)

        assert [r.tool for r in results] == ["gh"]
        assert not (rt / "ripgrep").exists()
        assert "ripgrep" not in rr.load_facts(rt)

    def test_provision_tool_stages_it_on_demand(self, served, tmp_path, target):
        root, base = served
        sha = _make_tar(root, "opt-gh.tar.gz", {"bin/gh": _script()})
        opt_sha = _make_tar(root, "opt-ripgrep.tar.gz", {"rg": _script()})
        pins = self._pins(tmp_path / "repo", base, sha, target, opt_sha)
        rt = tmp_path / "rt"

        result = rp.provision_tool("ripgrep", runtime_dir=rt, install_root=pins)

        assert (result.tool, result.action) == ("ripgrep", "downloaded")
        assert rr.load_facts(rt)["ripgrep"].version == "1.0.0"

    def test_the_sweep_owns_it_once_it_is_recorded(self, served, tmp_path, target):
        """A pin bump has to reach an optional tool the install DOES use."""
        root, base = served
        sha = _make_tar(root, "opt-gh.tar.gz", {"bin/gh": _script()})
        opt_sha = _make_tar(root, "opt-ripgrep.tar.gz", {"rg": _script()})
        pins = self._pins(tmp_path / "repo", base, sha, target, opt_sha)
        rt = tmp_path / "rt"
        rp.provision_tool("ripgrep", runtime_dir=rt, install_root=pins)

        # Same tool, new version: the sweep must pick the bump up.
        bumped_sha = _make_tar(
            root, "opt-ripgrep-2.tar.gz", {"rg": _script()}
        )
        bumped = _pins_file(
            tmp_path / "repo2",
            {
                "ripgrep": {
                    "version": "2.0.0",
                    "optional": True,
                    "files": {
                        target: {
                            "url": f"{base}/opt-ripgrep-2.tar.gz",
                            "sha256": bumped_sha,
                        }
                    },
                }
            },
        )
        results = rp.provision_runtimes(runtime_dir=rt, install_root=bumped)

        assert [(r.tool, r.action) for r in results] == [("ripgrep", "downloaded")]
        assert rr.load_facts(rt)["ripgrep"].version == "2.0.0"

    def test_provision_tool_brings_up_what_the_tool_extends(
        self, served, tmp_path, target
    ):
        """Staging runs the extended tool, so the chain comes first."""
        root, base = served
        base_sha = _make_tar(root, "chain-base.tar.gz", {"bin/gh": _script()})
        # 'ripgrep' extends 'gh': asking for ripgrep must stage gh too.
        opt_sha = _make_tar(root, "chain-ripgrep.tar.gz", {"rg": _script()})
        pins = _pins_file(
            tmp_path / "repo",
            {
                "gh": {
                    "version": "2.97.0",
                    "files": {
                        target: {"url": f"{base}/chain-base.tar.gz", "sha256": base_sha}
                    },
                },
                "ripgrep": {
                    "version": "1.0.0",
                    "optional": True,
                    "extends": ["gh"],
                    "files": {
                        target: {
                            "url": f"{base}/chain-ripgrep.tar.gz",
                            "sha256": opt_sha,
                        }
                    },
                },
            },
        )
        rt = tmp_path / "rt"

        result = rp.provision_tool("ripgrep", runtime_dir=rt, install_root=pins)

        assert result.ok, result.detail
        facts = rr.load_facts(rt)
        assert set(facts) == {"gh", "ripgrep"}


class TestCamoufoxPin:
    """The real pin-table entry, not a fixture.

    camoufox is the Camoufox BROWSER binary (~650MB of Firefox), the
    first optional tool. The npm module that drives it is not pinned
    here — it lives in scripts/camofox-browser with its own
    package-lock.json, because npm already pins a dependency tree by
    integrity hash and this table cannot express one.
    """

    def test_is_optional_and_needs_no_other_tool(self):
        pins = rr.load_pins()
        assert rr.is_optional("camoufox", pins), (
            "camoufox must stay optional: an install that never browses "
            "must not download a 650MB browser"
        )
        # A self-contained archive, unlike an npm package: nothing has to
        # be provisioned before it can be unpacked.
        assert pins["camoufox"].get("extends", []) == []

    def test_the_sweep_leaves_it_alone_until_it_is_installed(self):
        """The required set is what every install pays for."""
        pins = rr.load_pins()
        required = [t for t in rr.install_order(pins) if not rr.is_optional(t, pins)]
        assert "camoufox" not in required
        assert {"node", "npm", "uv", "git"}.issubset(set(required))

    def test_every_target_is_pinned_including_emulated_windows_arm(self):
        """Upstream ships no win arm64 build; ARM runs the x64 one."""
        pins = rr.load_pins()
        files = pins["camoufox"]["files"]
        assert set(files) == {
            "linux-x64", "linux-arm64",
            "darwin-x64", "darwin-arm64",
            "win32-x64", "win32-arm64",
        }
        assert files["win32-arm64"]["url"] == files["win32-x64"]["url"]

    def test_binary_layout_matches_the_launcher_camoufox_js_expects(self):
        """camoufox-js's LAUNCH_FILE map, per platform — inside the entry."""
        assert rp._binary_rel("camoufox", "linux-x64") == "camoufox-bin"
        assert rp._binary_rel("camoufox", "win32-x64") == "camoufox.exe"
        assert (
            rp._binary_rel("camoufox", "darwin-arm64")
            == "Camoufox.app/Contents/MacOS/camoufox"
        )

    def test_version_json_is_split_the_way_camoufox_js_reads_it(self):
        """Its fetcher greedily matches camoufox-(.+)-(.+)-<os>.<arch>.zip
        and builds Version(match[2], match[1]) — version first, release
        second. version.json must carry that same split back."""
        assert rp._camoufox_version_json("152.0.4-beta.28") == {
            "version": "152.0.4",
            "release": "beta.28",
        }

    def test_a_pin_without_a_release_half_is_rejected(self):
        """A malformed pin must fail loudly, not write a broken version.json."""
        with pytest.raises(ValueError, match="version.*release"):
            rp._camoufox_version_json("152.0.4")


class TestStaleReportingForOptionalTools:
    """An uninstalled optional tool is not drift.

    stale_tools() drives `hermes doctor` and the sealed-install
    require_current_runtimes() gate, so counting a capability nobody
    asked for as stale would make every install that does not browse
    report itself broken (it did: the nix runtime-dir check failed on
    exactly this).
    """

    @staticmethod
    def _pins(root: Path, target: str) -> Path:
        return _pins_file(
            root,
            {
                "gh": {
                    "version": "2.97.0",
                    "files": {target: {"url": "https://x/gh", "sha256": "0" * 64}},
                },
                "ripgrep": {
                    "version": "1.0.0",
                    "optional": True,
                    "files": {target: {"url": "https://x/rg", "sha256": "0" * 64}},
                },
            },
        )

    def test_uninstalled_optional_tool_is_not_stale(self, tmp_path, target):
        pins = self._pins(tmp_path / "repo", target)
        rt = tmp_path / "rt"

        drift = rp.stale_tools(runtime_dir=rt, install_root=pins)

        # gh is required and absent → drift. ripgrep is optional → not.
        assert "ripgrep" not in drift
        assert drift["gh"] == ("2.97.0", None)

    def test_an_installed_optional_tool_still_tracks_its_pin(
        self, served, tmp_path, target
    ):
        """Once installed, a bump has to be reported like any other tool."""
        root, base = served
        opt_sha = _make_tar(root, "stale-rg.tar.gz", {"rg": _script()})
        installed = _pins_file(
            tmp_path / "repo",
            {
                "ripgrep": {
                    "version": "1.0.0",
                    "optional": True,
                    "files": {
                        target: {"url": f"{base}/stale-rg.tar.gz", "sha256": opt_sha}
                    },
                }
            },
        )
        rt = tmp_path / "rt"
        rp.provision_tool("ripgrep", runtime_dir=rt, install_root=installed)

        bumped = _pins_file(
            tmp_path / "repo2",
            {
                "ripgrep": {
                    "version": "9.9.9",
                    "optional": True,
                    "files": {
                        target: {"url": f"{base}/stale-rg.tar.gz", "sha256": opt_sha}
                    },
                }
            },
        )
        drift = rp.stale_tools(runtime_dir=rt, install_root=bumped)

        assert drift["ripgrep"] == ("9.9.9", "1.0.0")


class TestSystemGitFirst:
    """Decision 1: a machine git clearing the flag floor beats a 147MB
    download. The floor (SYSTEM_GIT_FLOOR) is derived from the flags this
    codebase actually passes — scripts/audit-git-flags.py — because the
    only thing a floor buys is 'every argv we build will be understood'."""

    def _git_pins(self, served, tmp_path, target, name="git-sys.tar.gz"):
        root, base = served
        sha = _make_tar(root, name, {"bin/git": _script()})
        return _pins_file(tmp_path / f"repo-{name}", {"git": {
            "version": "2.53.0",
            "files": {target: {"url": f"{base}/{name}", "sha256": sha}}}})

    def _fake_system_git(self, tmp_path, version="2.44.1"):
        bin_dir = tmp_path / "sysbin"
        bin_dir.mkdir(exist_ok=True)
        git = bin_dir / "git"
        git.write_text(f"#!/bin/sh\necho 'git version {version}'\n")
        git.chmod(0o755)
        return git

    def test_a_floor_clearing_system_git_is_recorded_not_downloaded(
        self, served, tmp_path, target, monkeypatch
    ):
        pins = self._git_pins(served, tmp_path, target)
        rt, store = tmp_path / "rt", tmp_path / "store"
        system = self._fake_system_git(tmp_path, "2.44.1")
        monkeypatch.setattr(
            rp.shutil, "which", lambda n: str(system) if n == "git" else None
        )

        def no_downloads(*a, **k):
            raise AssertionError("system git accepted — nothing to download")

        monkeypatch.setattr(rp, "_download", no_downloads)

        results = rp.provision_runtimes(
            runtime_dir=rt, install_root=pins, store_dir=store
        )

        assert [(r.action, r.version) for r in results] == [("system", "2.44.1")]
        fact = rr.load_facts(rt)["git"]
        assert fact.source == "system"
        assert fact.path == str(system)  # absolute: no store entry exists
        assert list(store.glob("git-*")) == []

    def test_a_recorded_system_git_is_kept_while_it_still_works(
        self, served, tmp_path, target, monkeypatch
    ):
        pins = self._git_pins(served, tmp_path, target)
        rt, store = tmp_path / "rt", tmp_path / "store"
        system = self._fake_system_git(tmp_path)
        monkeypatch.setattr(
            rp.shutil, "which", lambda n: str(system) if n == "git" else None
        )
        rp.provision_runtimes(runtime_dir=rt, install_root=pins, store_dir=store)

        results = rp.provision_runtimes(
            runtime_dir=rt, install_root=pins, store_dir=store
        )

        assert [r.action for r in results] == ["kept"]

    def test_a_vanished_system_git_falls_back_to_the_pin(
        self, served, tmp_path, target, monkeypatch
    ):
        """Uninstalling the distro git must not brick the install: the
        next sweep provisions the pinned one instead of keeping a fact
        that points at nothing."""
        pins = self._git_pins(served, tmp_path, target)
        rt, store = tmp_path / "rt", tmp_path / "store"
        system = self._fake_system_git(tmp_path)
        monkeypatch.setattr(
            rp.shutil, "which", lambda n: str(system) if n == "git" else None
        )
        rp.provision_runtimes(runtime_dir=rt, install_root=pins, store_dir=store)

        system.unlink()
        monkeypatch.setattr(rp.shutil, "which", lambda n: None)

        results = rp.provision_runtimes(
            runtime_dir=rt, install_root=pins, store_dir=store
        )

        assert [(r.action, r.version) for r in results] == [
            ("downloaded", "2.53.0")
        ]
        assert rr.load_facts(rt)["git"].source == "managed"

    def test_a_below_floor_git_is_rejected(
        self, served, tmp_path, target, monkeypatch
    ):
        """git 2.30 would accept the probe and then choke on
        `rev-parse --path-format=absolute` (introduced 2.31) mid-flight.
        The floor exists to fail HERE, where the fix is a download."""
        pins = self._git_pins(served, tmp_path, target)
        rt, store = tmp_path / "rt", tmp_path / "store"
        old = self._fake_system_git(tmp_path, "2.30.2")
        monkeypatch.setattr(
            rp.shutil, "which", lambda n: str(old) if n == "git" else None
        )

        results = rp.provision_runtimes(
            runtime_dir=rt, install_root=pins, store_dir=store
        )

        assert [(r.action, r.version) for r in results] == [
            ("downloaded", "2.53.0")
        ]

    def test_a_system_fact_stays_off_the_managed_path_and_env(
        self, served, tmp_path, target, monkeypatch
    ):
        """/usr/bin must never ride into the managed PATH prefix (it
        would hoist every system binary above the pinned tools), and a
        system git gets no GIT_EXEC_PATH — it resolves its own helpers,
        and pointing it at ours would break it."""
        from installation import env as renv

        pins = self._git_pins(served, tmp_path, target)
        rt, store = tmp_path / "rt", tmp_path / "store"
        system = self._fake_system_git(tmp_path)
        monkeypatch.setattr(
            rp.shutil, "which", lambda n: str(system) if n == "git" else None
        )
        rp.provision_runtimes(runtime_dir=rt, install_root=pins, store_dir=store)

        assert renv.managed_path_dirs(rt, store_dir=store) == []
        env = renv.managed_tool_env(rt, store_dir=store)
        assert "GIT_EXEC_PATH" not in env

    def test_a_system_fact_is_not_drift(
        self, served, tmp_path, target, monkeypatch
    ):
        pins = self._git_pins(served, tmp_path, target)
        rt, store = tmp_path / "rt", tmp_path / "store"
        system = self._fake_system_git(tmp_path)
        monkeypatch.setattr(
            rp.shutil, "which", lambda n: str(system) if n == "git" else None
        )
        rp.provision_runtimes(runtime_dir=rt, install_root=pins, store_dir=store)

        assert rp.stale_tools(runtime_dir=rt, install_root=pins, store_dir=store) == {}

    def test_windows_never_takes_the_system_lane(self, monkeypatch):
        """PortableGit is load-bearing on Windows: bash.exe comes with
        it, and a winget git's bash may be missing or ASLR-broken."""
        monkeypatch.setattr(rp.sys, "platform", "win32")
        assert rp.probe_system_git() is None


class TestTermuxLane:
    """Decision 5: on Termux the pin table governs WHICH tools exist,
    but pkg supplies the bytes — verify-only, source="system", version
    floors instead of exact pins (pkg ships one rolling build)."""

    def _pins(self, tmp_path, target):
        return _pins_file(tmp_path / "repo", {"gh": {
            "version": "2.97.0",
            "files": {target: {
                "url": "https://example.com/never-fetched.tar.gz",
                "sha256": "0" * 64,
            }}}})

    def _termux(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMUX_VERSION", "0.118.0")
        bin_dir = tmp_path / "pkgbin"
        bin_dir.mkdir(exist_ok=True)
        return bin_dir

    def _pkg_tool(self, bin_dir, name, version):
        tool = bin_dir / name
        tool.write_text(f"#!/bin/sh\necho '{name} version {version}'\n")
        tool.chmod(0o755)
        return tool

    def test_a_pkg_tool_clearing_the_floor_is_recorded_not_downloaded(
        self, tmp_path, monkeypatch
    ):
        bin_dir = self._termux(monkeypatch, tmp_path)
        gh = self._pkg_tool(bin_dir, "gh", "2.62.0")
        monkeypatch.setattr(
            rp.shutil, "which", lambda n: str(gh) if n == "gh" else None
        )

        def no_downloads(*a, **k):
            raise AssertionError("nothing in the pin table runs on bionic")

        monkeypatch.setattr(rp, "_download", no_downloads)
        rt, store = tmp_path / "rt", tmp_path / "store"

        results = rp.provision_runtimes(
            runtime_dir=rt, install_root=self._pins(tmp_path, rr.current_target()),
            store_dir=store,
        )

        assert [(r.action, r.version) for r in results] == [("system", "2.62.0")]
        fact = rr.load_facts(rt)["gh"]
        assert fact.source == "system"
        assert not rp.stale_tools(
            runtime_dir=rt,
            install_root=self._pins(tmp_path, rr.current_target()),
            store_dir=store,
        )

    def test_a_missing_pkg_tool_fails_with_the_exact_install_line(
        self, tmp_path, monkeypatch
    ):
        """The failure IS the fix instruction: a Termux user cannot use
        a download URL, so the message must say `pkg install <name>`."""
        self._termux(monkeypatch, tmp_path)
        monkeypatch.setattr(rp.shutil, "which", lambda n: None)

        results = rp.provision_runtimes(
            runtime_dir=tmp_path / "rt",
            install_root=self._pins(tmp_path, rr.current_target()),
            store_dir=tmp_path / "store",
        )

        assert results[0].action == "failed"
        assert "pkg install gh" in (results[0].detail or "")

    def test_a_below_floor_pkg_tool_is_rejected_with_versions(
        self, tmp_path, monkeypatch
    ):
        bin_dir = self._termux(monkeypatch, tmp_path)
        old = self._pkg_tool(bin_dir, "gh", "1.9.0")
        monkeypatch.setattr(
            rp.shutil, "which", lambda n: str(old) if n == "gh" else None
        )

        results = rp.provision_runtimes(
            runtime_dir=tmp_path / "rt",
            install_root=self._pins(tmp_path, rr.current_target()),
            store_dir=tmp_path / "store",
        )

        assert results[0].action == "failed"
        detail = results[0].detail or ""
        assert "gh 1.9.0" in detail and ">= 2.0" in detail

    def test_an_unmapped_tool_is_explicitly_unsupported(
        self, tmp_path, monkeypatch
    ):
        """camoufox has no bionic build and no pkg package. Saying so
        beats a download that segfaults at first launch."""
        self._termux(monkeypatch, tmp_path)
        target = rr.current_target()
        pins = _pins_file(tmp_path / "repo2", {"camoufox": {
            "version": "1.0.0", "optional": True,
            "files": {target: {
                "url": "https://example.com/x.tar.gz", "sha256": "0" * 64,
            }}}})

        result = rp.provision_tool(
            "camoufox", runtime_dir=tmp_path / "rt",
            install_root=pins, store_dir=tmp_path / "store",
        )

        assert result.action == "failed"
        assert "not available on Termux" in (result.detail or "")
