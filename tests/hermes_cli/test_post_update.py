"""post_update step registry: scopes, isolation, and the migrate contract.

These assert behavior contracts, not snapshots: the registries' scopes must
match what boot_bootstrap gates them with, a failing step must not stop the
rest, and step_migrate_config must restore its backups when a migration
fails or does not advance the version.
"""
import json
from pathlib import Path

import pytest

from hermes_cli import post_update
from hermes_cli.post_update import (
    HOME_STEPS,
    MACHINE_STEPS,
    run_steps,
    step_migrate_config,
    step_state_db_guard,
)


# ── registry invariants ──────────────────────────────────────────────


def test_registries_are_disjoint_and_named():
    home_names = {name for name, _ in HOME_STEPS}
    machine_names = {name for name, _ in MACHINE_STEPS}
    assert home_names, "home registry must not be empty"
    assert not (home_names & machine_names)
    for name, func in (*HOME_STEPS, *MACHINE_STEPS):
        assert callable(func), name


def test_home_steps_cover_the_boot_contract():
    # boot_bootstrap gates these with the per-home record; the three
    # user-state concerns (config, skills, state.db) must all be present.
    names = {name for name, _ in HOME_STEPS}
    assert {"migrate_config", "sync_skills", "state_db_guard"} <= names


# ── run_steps isolation ──────────────────────────────────────────────


def test_run_steps_isolates_failures():
    order = []

    def ok():
        order.append("ok")
        return {"ok": True}

    def boom():
        order.append("boom")
        raise RuntimeError("nope")

    results = run_steps((("first", boom), ("second", ok)))
    assert order == ["boom", "ok"]  # failure did not stop the run
    assert results["first"]["ok"] is False
    assert "nope" in results["first"]["error"]
    assert results["second"] == {"ok": True}


# ── step_migrate_config ──────────────────────────────────────────────


def test_migrate_config_noop_when_current(monkeypatch):
    import hermes_cli.config as cfg

    monkeypatch.setattr(cfg, "check_config_version", lambda: (34, 34))
    result = step_migrate_config()
    assert result == {"ok": True, "skipped": "up-to-date"}


def test_migrate_config_restores_backup_when_version_does_not_advance(
    tmp_path, monkeypatch
):
    import hermes_cli.config as cfg
    import hermes_cli.config_migrations as mig

    config_path = tmp_path / "config.yaml"
    config_path.write_text("_config_version: 20\n", encoding="utf-8")
    env_path = tmp_path / ".env"

    floor = getattr(mig, "SUPPORT_FLOOR_VERSION", 12)
    versions = iter([(max(20, floor), 34), (max(20, floor), 34)])
    monkeypatch.setattr(cfg, "check_config_version", lambda: next(versions))
    monkeypatch.setattr(cfg, "get_config_path", lambda: config_path)
    monkeypatch.setattr(cfg, "get_env_path", lambda: env_path)

    def fake_migrate(**kw):
        # Corrupt the file; the version check will then report no advance.
        config_path.write_text("_config_version: 20\nbroken: true\n", encoding="utf-8")

    monkeypatch.setattr(cfg, "migrate_config", lambda **kw: fake_migrate(**kw))

    with pytest.raises(RuntimeError, match="did not advance"):
        step_migrate_config()

    # Original content restored from the backup.
    assert config_path.read_text(encoding="utf-8") == "_config_version: 20\n"
    backups = list(tmp_path.glob("config.yaml.bak-*"))
    assert backups, "backup file must exist"


def test_migrate_config_restores_backup_on_exception(tmp_path, monkeypatch):
    import hermes_cli.config as cfg
    import hermes_cli.config_migrations as mig

    config_path = tmp_path / "config.yaml"
    config_path.write_text("_config_version: 20\n", encoding="utf-8")

    floor = getattr(mig, "SUPPORT_FLOOR_VERSION", 12)
    monkeypatch.setattr(cfg, "check_config_version", lambda: (max(20, floor), 34))
    monkeypatch.setattr(cfg, "get_config_path", lambda: config_path)
    monkeypatch.setattr(cfg, "get_env_path", lambda: tmp_path / ".env")

    def exploding_migrate(**kw):
        config_path.write_text("half-written garbage", encoding="utf-8")
        raise RuntimeError("migration blew up")

    monkeypatch.setattr(cfg, "migrate_config", lambda **kw: exploding_migrate(**kw))

    with pytest.raises(RuntimeError, match="blew up"):
        step_migrate_config()
    assert config_path.read_text(encoding="utf-8") == "_config_version: 20\n"


# ── step_state_db_guard ──────────────────────────────────────────────


def test_state_db_guard_skips_missing_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert step_state_db_guard() == {"ok": True, "skipped": "no-state-db"}


def test_state_db_guard_flags_corrupt_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "state.db").write_text("this is not sqlite", encoding="utf-8")
    result = step_state_db_guard()
    assert result["ok"] is False
    assert result.get("error")


def test_state_db_guard_passes_valid_db(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    assert step_state_db_guard() == {"ok": True}


# ── machine-step registry ────────────────────────────────────────────


def test_provisioning_is_the_machine_scope_driver_path():
    """cua-driver has no refresh step of its own — provisioning carries it.

    The driver used to get a bespoke ``cua_driver_refresh`` machine step
    that shelled the upstream installer when a GitHub check confirmed a
    newer release. It is a pinned managed tool now, so the ordinary
    provisioner sweep moves it exactly like node or ripgrep, and a
    second mechanism would be a second authority on its version.
    """
    names = [name for name, _ in post_update.MACHINE_STEPS]
    assert "provision_runtimes" in names
    assert not any("cua" in name for name in names)


# ── __main__ entry ───────────────────────────────────────────────────


def test_main_reports_failure_in_exit_code(monkeypatch):
    monkeypatch.setattr(
        post_update, "HOME_STEPS",
        (("bad", lambda: (_ for _ in ()).throw(RuntimeError("x"))),),
    )
    monkeypatch.setattr(post_update, "MACHINE_STEPS", ())
    assert post_update.main(["--scope", "home"]) == 1


def test_main_scope_selects_registries(monkeypatch):
    ran = []
    monkeypatch.setattr(
        post_update, "HOME_STEPS", (("h", lambda: ran.append("h") or {"ok": True}),)
    )
    monkeypatch.setattr(
        post_update, "MACHINE_STEPS", (("m", lambda: ran.append("m") or {"ok": True}),)
    )
    assert post_update.main(["--scope", "home"]) == 0
    assert ran == ["h"]
    ran.clear()
    assert post_update.main(["--scope", "all"]) == 0
    assert ran == ["h", "m"]


# ── --update-phase runner mode ───────────────────────────────────────


def test_main_update_phase_delegates_with_parsed_flags(monkeypatch):
    """--update-phase routes to update_cmd._run_update_phase_inline with
    the CLI flags mapped through and NO windows resume token (the token
    is process-local to the parent). Modeled as the post-sync process
    (--resumed-after-sync): phase 1's sync/re-exec is its own test class
    below, and the real phase 2 always runs under this flag or after an
    in-place fall-through."""
    import hermes_cli.update_cmd as uc

    seen = {}

    def fake_phase(**kw):
        seen.update(kw)
        return 0

    monkeypatch.setattr(uc, "_run_update_phase_inline", fake_phase)
    rc = post_update.main([
        "--update-phase", "--resumed-after-sync", "--gateway-mode",
        "--assume-yes", "--pre-update-snapshot-id", "snap-123",
    ])
    assert rc == 0
    assert seen == {
        "gateway_mode": True,
        "assume_yes": True,
        "pre_update_snapshot_id": "snap-123",
        # Threaded through as an explicit parameter (the moved phase body
        # lost the enclosing scope that main's inline block read it from);
        # absent from the argv here, so it maps through as None.
        "pre_update_version": None,
        "windows_gateway_resume": None,
    }


def test_main_update_phase_propagates_exit_code(monkeypatch):
    import hermes_cli.update_cmd as uc

    monkeypatch.setattr(uc, "_run_update_phase_inline", lambda **kw: 1)
    assert post_update.main(["--update-phase"]) == 1


# ── _spawn_post_update_phase ─────────────────────────────────────────


def _spawn(monkeypatch, tmp_path, *, runner_exists=True, run_result=0, run_raises=None, **kw):
    """Drive update_cmd._spawn_post_update_phase with a fake subprocess."""
    import hermes_cli.main as hm
    import hermes_cli.update_cmd as uc

    root = tmp_path / "checkout"
    (root / "hermes_cli").mkdir(parents=True)
    if runner_exists:
        (root / "hermes_cli" / "post_update.py").write_text("# runner\n", encoding="utf-8")
    monkeypatch.setattr(hm, "PROJECT_ROOT", root)

    captured = {}

    def fake_run(cmd, **kwargs):
        if run_raises:
            raise run_raises
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        import subprocess as sp

        return sp.CompletedProcess(cmd, run_result)

    import subprocess as sp

    monkeypatch.setattr(sp, "run", fake_run)
    rc = uc._spawn_post_update_phase(
        gateway_mode=kw.get("gateway_mode", False),
        assume_yes=kw.get("assume_yes", False),
        pre_update_snapshot_id=kw.get("pre_update_snapshot_id"),
    )
    return rc, captured


def test_spawn_command_shape_and_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_DESKTOP_CHILD_PID", "424242")
    rc, cap = _spawn(
        monkeypatch, tmp_path,
        gateway_mode=True, assume_yes=True, pre_update_snapshot_id="snap-9",
    )
    assert rc == 0
    cmd = cap["cmd"]
    assert cmd[1:4] == ["-m", "hermes_cli.post_update", "--update-phase"]
    assert "--gateway-mode" in cmd and "--assume-yes" in cmd
    assert cmd[cmd.index("--pre-update-snapshot-id") + 1] == "snap-9"

    env = cap["kwargs"]["env"]
    # Inherit-and-extend: desktop contracts survive, unbuffered forced on.
    assert env["HERMES_DESKTOP_CHILD_PID"] == "424242"
    assert env["PYTHONUNBUFFERED"] == "1"
    # Inherited stdio: no capture/pipe arguments.
    assert "stdout" not in cap["kwargs"] and "capture_output" not in cap["kwargs"]


def test_spawn_returns_child_exit_code(monkeypatch, tmp_path):
    rc, _ = _spawn(monkeypatch, tmp_path, run_result=1)
    assert rc == 1


def test_spawn_none_when_runner_missing(monkeypatch, tmp_path):
    rc, _ = _spawn(monkeypatch, tmp_path, runner_exists=False)
    assert rc is None


def test_spawn_none_when_spawn_raises(monkeypatch, tmp_path):
    rc, _ = _spawn(monkeypatch, tmp_path, run_raises=OSError("no exec"))
    assert rc is None


# ── the re-exec-after-sync handoff (installer-redesign §B) ───────────


class _Args:
    """The argparse surface resync_and_reexec consumes."""

    def __init__(self, resumed=False, gateway=False, yes=False, snap=None):
        self.resumed_after_sync = resumed
        self.gateway_mode = gateway
        self.assume_yes = yes
        self.pre_update_snapshot_id = snap


class TestResyncAndReexec:
    def test_the_resumed_flag_is_absolute(self, monkeypatch):
        """Loop-proofing: the post-sync process must never sync again,
        even when the stamp looks stale (another writer could move it
        between exec and check — argv cannot race)."""

        def no_sync(*a, **k):
            raise AssertionError("--resumed-after-sync must not sync")

        from hermes_cli import venv_sync

        monkeypatch.setattr(venv_sync, "sync", no_sync)

        assert post_update.resync_and_reexec(_Args(resumed=True)) is None

    def test_current_and_sealed_run_phase2_in_place(self, monkeypatch):
        """No world change, no exec: the running interpreter is as good
        as a fresh one and the exec would only cost startup."""
        from hermes_cli import venv_sync

        for state in ("current", "sealed"):
            monkeypatch.setattr(
                venv_sync, "sync", lambda *a, s=state, **k: {"state": s, "ok": True}
            )
            monkeypatch.setattr(
                post_update.os, "execv",
                lambda *a: (_ for _ in ()).throw(AssertionError("must not exec")),
            )
            assert post_update.resync_and_reexec(_Args()) is None

    def test_a_failed_sync_stops_the_phase(self, monkeypatch):
        from hermes_cli import venv_sync

        monkeypatch.setattr(
            venv_sync,
            "sync",
            lambda *a, **k: {"state": "failed", "ok": False, "detail": "uv exited 3"},
        )

        assert post_update.resync_and_reexec(_Args()) == 1

    def test_a_synced_world_reexecs_with_carried_args(self, monkeypatch):
        """POSIX: os.execv, same pid, update-lock owner stays correct.
        Every serialized arg must survive the handoff, plus the resumed
        marker."""
        from hermes_cli import venv_sync

        monkeypatch.setattr(
            venv_sync, "sync", lambda *a, **k: {"state": "synced", "ok": True}
        )
        seen = {}

        def fake_execv(exe, argv):
            seen["exe"] = exe
            seen["argv"] = argv
            raise SystemExit(0)  # execv never returns; simulate the replacement

        monkeypatch.setattr(post_update.os, "execv", fake_execv)
        monkeypatch.setattr(post_update.os, "name", "posix")

        with pytest.raises(SystemExit):
            post_update.resync_and_reexec(
                _Args(gateway=True, yes=True, snap="snap-7")
            )

        argv = seen["argv"]
        assert seen["exe"] == post_update.sys.executable
        assert argv[1:4] == ["-m", "hermes_cli.post_update", "--update-phase"]
        assert "--resumed-after-sync" in argv
        assert "--gateway-mode" in argv and "--assume-yes" in argv
        assert argv[argv.index("--pre-update-snapshot-id") + 1] == "snap-7"

    def test_windows_spawns_and_propagates(self, monkeypatch):
        """No exec on Windows: spawn + wait + propagate the child's code;
        the child passes the update lock by ancestry."""
        from hermes_cli import venv_sync

        monkeypatch.setattr(
            venv_sync, "sync", lambda *a, **k: {"state": "synced", "ok": True}
        )
        monkeypatch.setattr(post_update.os, "name", "nt")
        seen = {}

        class _Done:
            returncode = 7

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _Done()

        monkeypatch.setattr(post_update.subprocess, "run", fake_run)

        assert post_update.resync_and_reexec(_Args()) == 7
        assert "--resumed-after-sync" in seen["argv"]
