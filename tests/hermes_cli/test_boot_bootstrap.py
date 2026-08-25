"""Boot-time post-update bootstrap: identity, records, locks, single-flight.

The record files are an optimization layer over idempotent steps; these
tests assert the contracts that keep that safe: identity resolution from
real git trees and stamps, record scoping (per-install AND per-home vs
per-machine), and the lock protocol including the double-check under lock.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import boot_bootstrap
from hermes_cli.boot_bootstrap import (
    _RecordLock,
    current_install_identity,
    needs_bootstrap,
    read_git_head,
    read_last_known,
    record_path,
    run_boot_bootstrap,
    write_record,
)


def _git(args, cwd):
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    })
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-b", "main"], root)
    (root / "f.txt").write_text("1", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-m", "one"], root)
    return root


def _head_sha(root):
    return _git(["rev-parse", "HEAD"], root).stdout.strip()


# ── read_git_head ────────────────────────────────────────────────────


def test_read_git_head_branch_ref(repo):
    assert read_git_head(repo) == _head_sha(repo)


def test_read_git_head_detached(repo):
    sha = _head_sha(repo)
    _git(["checkout", "--detach", sha], repo)
    assert read_git_head(repo) == sha


def test_read_git_head_packed_refs(repo):
    sha = _head_sha(repo)
    _git(["pack-refs", "--all"], repo)
    # Loose ref is gone; only packed-refs carries the branch now.
    assert not (repo / ".git" / "refs" / "heads" / "main").exists()
    assert read_git_head(repo) == sha


def test_read_git_head_worktree_gitfile(repo, tmp_path):
    wt = tmp_path / "wt"
    _git(["worktree", "add", str(wt)], repo)
    assert (wt / ".git").is_file()  # gitfile pointer, not a directory
    assert read_git_head(wt) == _head_sha(wt)


def test_read_git_head_reftable(tmp_path):
    """The repo format that killed the hand-rolled parser.

    A reftable repo stores refs in neither loose files nor packed-refs,
    and its HEAD is a decoy (``ref: refs/heads/.invalid``) kept only so
    pre-reftable tools fail loudly instead of misreading. The old file
    parser returned None on every such repo; asking git answers. If git
    here is too old for reftable, there is nothing to test.
    """
    root = tmp_path / "rt"
    root.mkdir()
    try:
        _git(["init", "--ref-format=reftable", "-b", "main", "."], root)
    except subprocess.CalledProcessError:
        pytest.skip("git too old for --ref-format=reftable")
    (root / "f.txt").write_text("1", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-m", "one"], root)

    head = (root / ".git" / "HEAD").read_text(encoding="utf-8")
    assert ".invalid" in head, "reftable decoy HEAD is the point of this test"

    assert read_git_head(root) == _head_sha(root)


def test_read_git_head_missing_and_garbage(tmp_path):
    assert read_git_head(tmp_path) is None
    (tmp_path / ".git").write_text("not a gitdir pointer", encoding="utf-8")
    assert read_git_head(tmp_path) is None


# ── current_install_identity ─────────────────────────────────────────


def test_identity_prefers_git(repo):
    assert current_install_identity(repo) == _head_sha(repo)


def test_identity_sealed_stamp(tmp_path):
    (tmp_path / "install-stamp.json").write_text(
        json.dumps({"commit": "a" * 40, "distribution": "desktop-app", "updateMechanism": "electron-updater"}),
        encoding="utf-8",
    )
    assert current_install_identity(tmp_path) == "a" * 40


def test_identity_broken_tree_is_none(tmp_path):
    assert current_install_identity(tmp_path) is None
    (tmp_path / "install-stamp.json").write_text("garbage", encoding="utf-8")
    assert current_install_identity(tmp_path) is None


# ── record paths ─────────────────────────────────────────────────────


def test_record_paths_key_on_install_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    a = record_path(tmp_path / "install-a", "home")
    b = record_path(tmp_path / "install-b", "home")
    assert a != b
    # The key is a FOLDER now (installs/<SHA16>/bootstrap/<profile>.json),
    # not a filename suffix: same grandparent tree, different key dirs.
    assert a.parent != b.parent
    assert a.parent.parent.parent == b.parent.parent.parent  # installs/
    assert a.name == b.name  # the profile filename is the shared part


def test_home_records_differ_per_profile_machine_record_shared(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    base = tmp_path / ".hermes"
    profile = base / "profiles" / "coder"
    install = tmp_path / "install"

    monkeypatch.setenv("HERMES_HOME", str(base))
    home_default = record_path(install, "home")
    machine_default = record_path(install, "machine")

    monkeypatch.setenv("HERMES_HOME", str(profile))
    home_profile = record_path(install, "home")
    machine_profile = record_path(install, "machine")

    assert home_default != home_profile  # each profile bootstraps its own home
    assert machine_default == machine_profile  # machine record is shared


def test_record_path_rejects_unknown_scope(tmp_path):
    with pytest.raises(ValueError):
        record_path(tmp_path, "galaxy")


def test_symlinked_root_canonicalizes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    real = tmp_path / "real-install"
    real.mkdir()
    link = tmp_path / "link-install"
    link.symlink_to(real)
    assert record_path(real, "home") == record_path(link, "home")


# ── needs_bootstrap ──────────────────────────────────────────────────


def test_needs_bootstrap_lifecycle(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    sha = _head_sha(repo)

    # No record yet → identity returned.
    assert needs_bootstrap(repo, "home") == sha

    write_record(repo, "home", sha)
    assert needs_bootstrap(repo, "home") is None

    # New commit → mismatch again.
    (repo / "f.txt").write_text("2", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", "two"], repo)
    assert needs_bootstrap(repo, "home") == _head_sha(repo)


def test_needs_bootstrap_broken_tree_never_fires(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    assert needs_bootstrap(tmp_path / "nope", "home") is None


# ── lock protocol ────────────────────────────────────────────────────


def test_lock_loser_skips(tmp_path):
    record = tmp_path / "r.json"
    first = _RecordLock(record)
    second = _RecordLock(record)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_stale_lock_is_broken(tmp_path):
    record = tmp_path / "r.json"
    lock_path = record.with_name(record.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": 1, "startedAt": time.time() - 3600}), encoding="utf-8"
    )
    lock = _RecordLock(record)
    assert lock.acquire()
    lock.release()


def test_fresh_lock_is_respected(tmp_path):
    record = tmp_path / "r.json"
    lock_path = record.with_name(record.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "startedAt": time.time()}), encoding="utf-8"
    )
    assert not _RecordLock(record).acquire()


# ── run_boot_bootstrap ───────────────────────────────────────────────


@pytest.fixture
def fake_steps(monkeypatch):
    calls = {"home": 0, "machine": 0}

    def home_step():
        calls["home"] += 1
        return {"ok": True}

    def machine_step():
        calls["machine"] += 1
        return {"ok": True}

    from hermes_cli import post_update

    monkeypatch.setattr(post_update, "HOME_STEPS", (("h", home_step),))
    monkeypatch.setattr(post_update, "MACHINE_STEPS", (("m", machine_step),))
    # Machine steps run on a thread; make them synchronous for the test.
    # boot_bootstrap does `import threading` inside the function, so patching
    # the stdlib module's Thread attribute is what reaches it.
    import threading

    class _SyncThread:
        def __init__(self, target=None, args=(), **kw):
            self._target, self._args = target, args

        def start(self):
            if self._target is not None:
                self._target(*self._args)

    monkeypatch.setattr(threading, "Thread", _SyncThread)
    return calls


def test_run_boot_bootstrap_runs_then_noops(repo, tmp_path, monkeypatch, fake_steps):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    first = run_boot_bootstrap(repo)
    assert fake_steps == {"home": 1, "machine": 1}
    assert first["home"] == {"h": {"ok": True}}
    assert first["machine"] == "deferred"

    second = run_boot_bootstrap(repo)
    assert fake_steps == {"home": 1, "machine": 1}  # no re-run
    assert second == {"home": "skipped", "machine": "skipped"}


def test_machine_step_runs_once_across_profiles(repo, tmp_path, monkeypatch, fake_steps):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    base = tmp_path / ".hermes"

    monkeypatch.setenv("HERMES_HOME", str(base))
    run_boot_bootstrap(repo)
    monkeypatch.setenv("HERMES_HOME", str(base / "profiles" / "coder"))
    run_boot_bootstrap(repo)

    # Each home bootstraps itself; the machine step fires once.
    assert fake_steps == {"home": 2, "machine": 1}


def test_step_failure_still_writes_record(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    from hermes_cli import post_update

    def boom():
        raise RuntimeError("step exploded")

    monkeypatch.setattr(post_update, "HOME_STEPS", (("boom", boom),))
    monkeypatch.setattr(post_update, "MACHINE_STEPS", ())

    run_boot_bootstrap(repo)
    record = read_last_known(record_path(repo, "home"))
    assert record["identity"] == _head_sha(repo)
    assert record["results"]["boom"]["ok"] is False

    # A broken step must not retrigger the slow path every boot.
    assert needs_bootstrap(repo, "home") is None


def test_double_check_under_lock(repo, tmp_path, monkeypatch, fake_steps):
    """A racer that finished between our read and our acquire wins."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    sha = _head_sha(repo)

    real_acquire = _RecordLock.acquire

    def acquire_after_racer_finished(self):
        got = real_acquire(self)
        if got and self.path.name.endswith(".json.lock"):
            # Simulate the previous holder completing just before us.
            write_record(repo, "home", sha)
        return got

    monkeypatch.setattr(_RecordLock, "acquire", acquire_after_racer_finished)
    result = run_boot_bootstrap(repo)
    assert result["home"] == "done-by-other"
    assert fake_steps["home"] == 0


def test_maybe_run_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        boot_bootstrap, "run_boot_bootstrap",
        lambda root: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    boot_bootstrap.maybe_run_boot_bootstrap(tmp_path)  # must not raise


def test_deferred_machine_steps_execute(repo, tmp_path, monkeypatch):
    """Stamp-gap item 4: the machine scope defers to a REAL thread — the
    record is written first (boot readiness must not wait on network
    installers), and the steps still actually run. No synchronous-thread
    fake here on purpose: the claim under test is that the deferred work
    happens, not that the registry is wired."""
    import threading

    from hermes_cli import post_update

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    ran = threading.Event()
    monkeypatch.setattr(post_update, "HOME_STEPS", ())
    monkeypatch.setattr(
        post_update,
        "MACHINE_STEPS",
        (("probe", lambda: (ran.set(), {"ok": True})[1]),),
    )

    result = run_boot_bootstrap(repo)

    assert result["machine"] == "deferred"
    # The record was written BEFORE the steps finished (that ordering is
    # the design: a crash mid-step must not retrigger every boot).
    record = boot_bootstrap.record_path(repo, "machine")
    assert json.loads(record.read_text())["results"]["deferred"] is True
    assert ran.wait(timeout=10), "deferred machine steps never executed"


def test_sealed_tree_bootstrap_end_to_end(tmp_path, monkeypatch):
    """Stamp-gap item 5: the desktop-bundle-swap scenario. A sealed tree
    (install-stamp.json, no .git) must bootstrap on first boot, no-op on
    the second, and RE-RUN when the stamp's commit changes — that is the
    only signal a bundle swap emits."""
    import json as _json

    from hermes_cli import post_update

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    sealed = tmp_path / "payload"
    sealed.mkdir()
    (sealed / "install-stamp.json").write_text(
        _json.dumps({"commit": "aaaa1111", "payload": "full", "updateMechanism": "electron-updater"})
    )

    calls = {"n": 0}

    def count():
        calls["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(post_update, "HOME_STEPS", (("h", count),))
    monkeypatch.setattr(post_update, "MACHINE_STEPS", ())

    assert run_boot_bootstrap(sealed)["home"] != "skipped"
    assert calls["n"] == 1
    assert run_boot_bootstrap(sealed)["home"] == "skipped"
    assert calls["n"] == 1  # second boot: identity unchanged, no work

    # The bundle swap: same root, new stamp commit.
    (sealed / "install-stamp.json").write_text(
        _json.dumps({"commit": "bbbb2222", "payload": "full", "updateMechanism": "electron-updater"})
    )

    assert run_boot_bootstrap(sealed)["home"] != "skipped"
    assert calls["n"] == 2, "a swapped bundle must re-run the bootstrap"


# ── the per-install state folder (doc4 §B) ──────────────────────────


def test_ensure_install_dir_writes_the_reverse_map(repo, tmp_path, monkeypatch):
    """install.json is the sha16 → root reverse map that makes orphan GC
    possible. Written once; a second call must not rewrite firstSeen."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    state = boot_bootstrap.ensure_install_dir(repo)

    assert state == boot_bootstrap.install_state_dir(repo)
    record = json.loads((state / "install.json").read_text())
    assert record["root"] == str(repo.resolve())
    assert record["steward"] == "checkout"
    first_seen = record["firstSeen"]

    boot_bootstrap.ensure_install_dir(repo)
    assert json.loads((state / "install.json").read_text())["firstSeen"] == first_seen


def test_orphan_sweep_flags_only_vanished_roots(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    boot_bootstrap.ensure_install_dir(repo)  # alive
    ghost = tmp_path / "deleted-checkout"
    ghost.mkdir()
    ghost_state = boot_bootstrap.ensure_install_dir(ghost)
    shutil.rmtree(ghost)  # the install is gone; its state folder is not

    orphans = boot_bootstrap.orphaned_installs()

    assert [(folder, root) for folder, root in orphans] == [
        (ghost_state, str(ghost))
    ]


def test_bootstrap_records_live_inside_the_state_folder(repo, tmp_path, monkeypatch):
    """Both scopes share the install's folder; profile identity rides the
    FILENAME. One anchor, not two homes."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    home = boot_bootstrap.record_path(repo, "home")
    machine = boot_bootstrap.record_path(repo, "machine")

    state = boot_bootstrap.install_state_dir(repo)
    assert home == state / "bootstrap" / "default.json"
    assert machine == state / "bootstrap" / "machine.json"


class TestSealedDriftBackstop:
    """_report_sealed_runtime_drift — every boot of a drifted sealed tree
    says so; nothing else makes a sound, and nothing ever gates boot."""

    def _sealed_tree(self, tmp_path):
        root = tmp_path / "sealed"
        root.mkdir()
        (root / "install-stamp.json").write_text(
            json.dumps({"schemaVersion": 2, "commit": "a" * 40, "distribution": "docker", "updateMechanism": "external"}),
            encoding="utf-8",
        )
        return root

    def test_drifted_sealed_tree_reports_to_stderr(self, tmp_path, capsys, monkeypatch):
        import installation.provisioner as prov

        root = self._sealed_tree(tmp_path)
        monkeypatch.setattr(
            prov, "stale_tools", lambda **kw: {"node": ("26.7.0", "26.5.1")}
        )
        message = boot_bootstrap._report_sealed_runtime_drift(root)
        assert message is not None and "node" in message
        err = capsys.readouterr().err
        assert "docker" in err and "26.7.0" in err and "26.5.1" in err

    def test_current_sealed_tree_is_silent(self, tmp_path, capsys, monkeypatch):
        import installation.provisioner as prov

        root = self._sealed_tree(tmp_path)
        monkeypatch.setattr(prov, "stale_tools", lambda **kw: {})
        assert boot_bootstrap._report_sealed_runtime_drift(root) is None
        assert capsys.readouterr().err == ""

    def test_checkout_is_silent_even_with_drift(self, repo, capsys, monkeypatch):
        """A checkout provisions on demand; drift there is self-healing
        and must not produce boot noise."""
        import installation.provisioner as prov

        monkeypatch.setattr(
            prov, "stale_tools", lambda **kw: {"node": ("26.7.0", None)}
        )
        assert boot_bootstrap._report_sealed_runtime_drift(repo) is None
        assert capsys.readouterr().err == ""

    def test_a_broken_check_never_gates_boot(self, tmp_path, monkeypatch):
        import installation.provisioner as prov

        root = self._sealed_tree(tmp_path)

        def explode(**kw):
            raise RuntimeError("facts file corrupted")

        monkeypatch.setattr(prov, "stale_tools", explode)
        assert boot_bootstrap._report_sealed_runtime_drift(root) is None

    def test_drift_lands_in_the_boot_summary(self, tmp_path, capsys, monkeypatch):
        import installation.provisioner as prov

        root = self._sealed_tree(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        monkeypatch.setattr(
            prov, "stale_tools", lambda **kw: {"uv": ("0.12.3", "0.11.6")}
        )
        summary = run_boot_bootstrap(root)
        assert "uv" in summary.get("sealed_runtime_drift", "")


class TestOrphanedStoreEntries:
    """orphaned_store_entries — facts are the only authority, keep on doubt."""

    def _publish(self, store, name, payload=b"tool bytes"):
        entry = store / name
        entry.mkdir(parents=True)
        (entry / "tool.bin").write_bytes(payload)
        (entry / ".hermes-store-entry.json").write_text("{}", encoding="utf-8")
        return entry

    def _install_with_facts(self, tmp_path, name, facts: dict):
        """A live install root whose runtimes.json references *facts*."""
        from installation.registry import RuntimeFact, save_facts

        root = tmp_path / name
        rt = root / ".hermes-runtime"
        rt.mkdir(parents=True)
        save_facts(
            {t: RuntimeFact(version="1.0.0", path=p) for t, p in facts.items()},
            rt,
        )
        boot_bootstrap.ensure_install_dir(root)
        return root

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        store = tmp_path / ".hermes" / "tools"
        store.mkdir(parents=True)
        import installation.paths as ip

        monkeypatch.setattr(ip, "get_tool_store", lambda: store)
        return store

    def test_referenced_entries_survive_unreferenced_are_flagged(
        self, store, tmp_path
    ):
        self._publish(store, "ripgrep-15.2.0-linux-x64")
        stale = self._publish(store, "ripgrep-14.1.0-linux-x64", b"x" * 512)
        self._install_with_facts(
            tmp_path, "live-install",
            {"ripgrep": "ripgrep-15.2.0-linux-x64/rg"},
        )

        orphans = boot_bootstrap.orphaned_store_entries()

        assert [(e.name, s) for e, s in orphans] == [
            ("ripgrep-14.1.0-linux-x64", 512 + 2)  # tool.bin + "{}" marker
        ]
        assert stale.exists()  # report-only: nothing was deleted

    def test_any_live_installs_reference_keeps_an_entry(self, store, tmp_path):
        """Two installs, one old, one new — BOTH versions stay."""
        self._publish(store, "uv-0.12.3-linux-x64")
        self._publish(store, "uv-0.11.6-linux-x64")
        self._install_with_facts(tmp_path, "fresh", {"uv": "uv-0.12.3-linux-x64/uv"})
        self._install_with_facts(tmp_path, "behind", {"uv": "uv-0.11.6-linux-x64/uv"})

        assert boot_bootstrap.orphaned_store_entries() == []

    def test_unreadable_facts_err_toward_keep(self, store, tmp_path):
        """A busted install must cost disk, not break its neighbours."""
        self._publish(store, "gh-2.97.0-linux-x64")
        root = tmp_path / "busted"
        (root / ".hermes-runtime").mkdir(parents=True)
        (root / ".hermes-runtime" / "runtimes.json").write_text(
            "NOT JSON", encoding="utf-8"
        )
        boot_bootstrap.ensure_install_dir(root)

        # gh COULD belong to the busted install; with its facts unreadable
        # the answer must be "keep", not "orphan".
        assert boot_bootstrap.orphaned_store_entries() == []

    def test_unpublished_dirs_are_not_candidates(self, store, tmp_path):
        """Scratch dirs and stray files are the provisioner's cleanup, and
        a store doubling as a facts dir holds runtimes.json — none of it
        may show up as deletable."""
        scratch = store / ".staging-abc123"
        scratch.mkdir()
        (store / "runtimes.json").write_text("{}", encoding="utf-8")

        assert boot_bootstrap.orphaned_store_entries() == []
