"""venv_sync must work on trees where the venv does not exist yet.

It is the second stdlib-only half of "make this install runnable" (the
provisioner is the first): the installers call it on a fresh clone
before any dependency is importable, and post_update calls it after a
tree swap when the venv is not trustworthy. Its behaviour is driven
with a FAKE uv on PATH — the module's job is deciding whether/how to
call uv and what to record, not resolving packages, and a fake makes
every decision observable (argv, env, cwd) without network.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import venv_sync
from tests.test_installation_stdlib_only import run_bare

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── the stdlib-only contract, RUN not parsed ─────────────────────────────


class TestStdlibOnly:
    def test_imports_and_answers_bare(self, tmp_path):
        """The whole CLI surface must survive a stripped interpreter."""
        result = run_bare(
            f"""
            from hermes_cli import venv_sync
            root = {str(tmp_path)!r}
            out = venv_sync.sync(root, check=True)
            assert out["state"] == "failed", out  # empty dir: no pyproject
            print("ok")
            """
        )
        assert result.returncode == 0, result.stderr

    def test_no_third_party_module_ends_up_loaded(self):
        """Importing it must not drag in ANY non-stdlib module.

        Same doctrine as the installation package's audit: check
        sys.modules after import, because a parser cannot see lazy or
        conditional imports fire.
        """
        result = run_bare(
            """
            import sys
            import hermes_cli.venv_sync
            loaded = {
                name.split(".")[0]
                for name, mod in sys.modules.items()
                if mod is not None and getattr(mod, "__file__", None)
            }
            stdlib = set(sys.stdlib_module_names)
            foreign = {
                n for n in loaded
                if n not in stdlib and n not in ("hermes_cli",)
            }
            assert not foreign, f"venv_sync loaded non-stdlib: {foreign}"
            print("ok")
            """
        )
        assert result.returncode == 0, result.stderr


# ── behaviour, driven through a fake uv ──────────────────────────────────


def _fake_uv(tmp_path: Path, exit_code: int = 0) -> Path:
    """A uv that records its invocation and exits as told."""
    record = tmp_path / "uv-calls.jsonl"
    script = tmp_path / "uv"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"with open({str(record)!r}, 'a') as f:\n"
        "    f.write(json.dumps({\n"
        "        'argv': sys.argv[1:],\n"
        "        'cwd': os.getcwd(),\n"
        "        'venv_env': os.environ.get('UV_PROJECT_ENVIRONMENT'),\n"
        "        'virtual_env': os.environ.get('VIRTUAL_ENV'),\n"
        "    }) + '\\n')\n"
        f"raise SystemExit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return record


def _checkout(tmp_path: Path, name: str = "co") -> Path:
    """A minimal tree venv_sync classifies as a checkout."""
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "uv.lock").write_text("lock-v1\n")
    return root


def _wire_uv(monkeypatch, tmp_path: Path, root: Path, exit_code: int = 0) -> Path:
    """Point the managed-uv fact at a fake binary; return its call log."""
    record = _fake_uv(tmp_path, exit_code)
    rt = root / ".hermes-runtime"
    rt.mkdir(exist_ok=True)
    (rt / "runtimes.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "tools": {"uv": {"version": "0.12.3", "path": "uv-fake/uv"}},
            }
        )
    )
    entry = rt / "uv-fake"
    entry.mkdir(exist_ok=True)
    fake = tmp_path / "uv"
    (entry / "uv").symlink_to(fake)
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(rt))
    return record


class TestCheckoutSync:
    def test_a_stale_checkout_syncs_locked_against_its_own_venv(
        self, tmp_path, monkeypatch
    ):
        root = _checkout(tmp_path)
        record = _wire_uv(monkeypatch, tmp_path, root)

        out = venv_sync.sync(root)

        assert out == {"state": "synced", "ok": True}
        call = json.loads(record.read_text().splitlines()[0])
        assert call["argv"] == ["sync", "--extra", "all", "--locked"]
        assert call["venv_env"] == str(root / "venv")
        assert call["virtual_env"] is None  # caller's venv must not leak in
        assert call["cwd"] == str(root)

    def test_a_current_checkout_never_invokes_uv(self, tmp_path, monkeypatch):
        """The stamp is the fast path: currency costs a file read."""
        root = _checkout(tmp_path)
        record = _wire_uv(monkeypatch, tmp_path, root)

        assert venv_sync.sync(root)["state"] == "synced"
        assert venv_sync.sync(root)["state"] == "current"
        assert len(record.read_text().splitlines()) == 1  # one call total

    def test_an_edited_pyproject_invalidates_the_stamp(
        self, tmp_path, monkeypatch
    ):
        """Extras edits without a lock bump still re-sync."""
        root = _checkout(tmp_path)
        _wire_uv(monkeypatch, tmp_path, root)
        assert venv_sync.sync(root)["state"] == "synced"

        (root / "pyproject.toml").write_text("[project]\nname='y'\n")

        assert venv_sync.sync(root)["state"] == "synced"

    def test_a_failed_sync_writes_no_stamp(self, tmp_path, monkeypatch):
        """A failure must leave the next run trying again, not skipping."""
        root = _checkout(tmp_path)
        _wire_uv(monkeypatch, tmp_path, root, exit_code=3)

        out = venv_sync.sync(root)

        assert out["state"] == "failed" and "3" in out["detail"]
        assert venv_sync.read_stamp(root) == {}

    def test_check_mode_reports_and_changes_nothing(self, tmp_path, monkeypatch):
        root = _checkout(tmp_path)
        record = _wire_uv(monkeypatch, tmp_path, root)

        out = venv_sync.sync(root, check=True)

        assert out["state"] == "would-sync"
        assert not record.exists()  # no uv call
        assert venv_sync.read_stamp(root) == {}

    def test_no_managed_uv_is_a_failure_that_names_the_fix(
        self, tmp_path, monkeypatch
    ):
        root = _checkout(tmp_path)
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(root / ".hermes-runtime"))

        out = venv_sync.sync(root)

        assert out["state"] == "failed"
        assert "provisioner" in out["detail"]


class TestSealedTrees:
    def test_a_sealed_tree_is_a_clean_noop(self, tmp_path, monkeypatch):
        """The desktop payload and nix bundle must not fail, must not sync."""
        root = tmp_path / "sealed"
        root.mkdir()
        (root / "install-stamp.json").write_text(
            json.dumps({"commit": "abc123", "payload": "full", "updateMechanism": "electron-updater"})
        )
        record = _fake_uv(tmp_path)

        out = venv_sync.sync(root)

        assert out == {"state": "sealed", "ok": True}
        assert not record.exists()

    def test_a_dev_tree_with_both_stamp_and_git_is_a_checkout(
        self, tmp_path, monkeypatch
    ):
        root = _checkout(tmp_path)
        (root / "install-stamp.json").write_text(
            json.dumps({"commit": "abc", "updateMechanism": "electron-updater"})
        )
        _wire_uv(monkeypatch, tmp_path, root)

        assert venv_sync.sync(root)["state"] == "synced"


class TestCliContract:
    def test_json_output_and_exit_codes(self, tmp_path, monkeypatch):
        """post_update and the installers read exactly this."""
        root = tmp_path / "sealed"
        root.mkdir()
        (root / "install-stamp.json").write_text(
            json.dumps({"commit": "x", "updateMechanism": "electron-updater"})
        )

        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_cli.venv_sync",
                "--project-root",
                str(root),
                "--json",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )

        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == {"state": "sealed", "ok": True}

    def test_failure_exits_nonzero(self, tmp_path):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_cli.venv_sync",
                "--project-root",
                str(tmp_path),  # empty dir: no pyproject, no stamp
                "--json",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )

        assert proc.returncode == 1
        assert json.loads(proc.stdout)["state"] == "failed"
