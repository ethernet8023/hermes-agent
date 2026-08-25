"""pm end-to-end: install a fake tool from a loopback server, prove the
lockfile/installed-state split, env composition, single-flight, adoption,
and gc behavior. Real downloads, real archives, real locks — no mocked
stores."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

import pm.paths as paths
import pm.registry as registry
from pm.lock import Facts, Lockfile
from pm.package import InstallError, Package, StatePackage, compose_env
from pm.packages import BinaryPackage
from pm.store import Store, current_target, flatten_single_dir


class FakeTool(BinaryPackage):
    name = "faketool"
    probe_version = False
    binary_rel = {"win32": "bin/faketool", "posix": "bin/faketool"}

    def fetch_url(self, version, target):
        return f"{FakeTool.base_url}/{self.name}-{version}.tar.gz"


class DepTool(FakeTool):
    name = "deptool"

    def env(self, entry, target):
        diff = super().env(entry, target)
        diff["DEPTOOL_SEEN"] = "1"
        return diff


class TopTool(FakeTool):
    name = "toptool"
    deps = ("deptool",)


def make_tar(docroot: Path, name: str, files: dict[str, str]) -> tuple[str, str]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    payload = buf.getvalue()
    (docroot / name).write_bytes(payload)
    return name, hashlib.sha256(payload).hexdigest()


@pytest.fixture
def served(tmp_path):
    docroot = tmp_path / "www"
    docroot.mkdir()
    handler = partial(SimpleHTTPRequestHandler, directory=str(docroot))
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield docroot, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def pm_env(tmp_path, served, monkeypatch):
    docroot, base_url = served
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    # Policy is pinned open here; the disabled-path tests pin it closed.
    import importlib

    ensure_mod = importlib.import_module("pm.ensure")
    monkeypatch.setattr(ensure_mod, "lazy_installs_allowed", lambda: True)
    paths._stamp.cache_clear()

    saved = dict(registry._packages)
    registry._packages.clear()
    for cls in (FakeTool, DepTool, TopTool):
        registry._packages[cls.name] = cls()
    FakeTool.base_url = base_url

    lock_dir = tmp_path / "repo-lock"
    lock_dir.mkdir()
    lockfile_path = lock_dir / "lock.json"
    monkeypatch.setattr(paths, "lockfile_path", lambda: lockfile_path)

    name, digest = make_tar(docroot, "faketool-1.0.tar.gz", {"bin/faketool": "#!x"})
    lockfile = Lockfile(lockfile_path)
    lockfile.set_pin("faketool", "1.0", {"any": digest})
    lockfile.save()

    yield lockfile_path, runtime, docroot, base_url

    registry._packages.clear()
    registry._packages.update(saved)


def _pin(lockfile_path: Path, name: str, version: str, digest: str) -> None:
    lockfile = Lockfile(lockfile_path)
    lockfile.set_pin(name, version, {"any": digest})
    lockfile.save()


def test_install_and_env(pm_env):
    from pm.ensure import ensure, is_installed

    runner = ensure("faketool", base_env={})
    assert is_installed("faketool")
    assert "faketool-1.0" in runner.env["PATH"]


def test_idempotent_and_offline_after_install(pm_env):
    from pm.ensure import ensure

    _, _, docroot, _ = pm_env
    ensure("faketool", base_env={})
    (docroot / "faketool-1.0.tar.gz").unlink()
    runner = ensure("faketool", base_env={})
    assert "faketool-1.0" in runner.env["PATH"]


def test_bad_hash_rejected(pm_env):
    from pm.ensure import ensure

    lockfile_path, *_ = pm_env
    _pin(lockfile_path, "faketool", "1.0", "0" * 64)
    with pytest.raises(InstallError, match="download failed"):
        ensure("faketool", base_env={})


def test_fetch_is_a_store_entry(pm_env):
    from pm.ensure import ensure

    _, runtime, docroot, _ = pm_env
    ensure("faketool", base_env={})
    fetches = [p for p in runtime.iterdir() if p.name.startswith("fetch-")]
    assert len(fetches) == 1


def test_deps_compose_dependents_win(pm_env):
    from pm.ensure import ensure

    lockfile_path, _, docroot, _ = pm_env
    name, digest = make_tar(docroot, "deptool-1.0.tar.gz", {"bin/faketool": "y"})
    _pin(lockfile_path, "deptool", "1.0", digest)
    name, digest = make_tar(docroot, "toptool-1.0.tar.gz", {"bin/faketool": "z"})
    _pin(lockfile_path, "toptool", "1.0", digest)

    runner = ensure("toptool", base_env={})
    assert runner.env["DEPTOOL_SEEN"] == "1"
    path = runner.env["PATH"]
    assert path.index("toptool-1.0") < path.index("deptool-1.0")


def test_version_bump_reinstalls_and_migrates(pm_env):
    from pm.ensure import ensure

    lockfile_path, _, docroot, _ = pm_env
    migrations = []
    registry._packages["faketool"].migrate = lambda prev, new: migrations.append((prev, new))

    ensure("faketool", base_env={})
    name, digest = make_tar(docroot, "faketool-2.0.tar.gz", {"bin/faketool": "#!2"})
    _pin(lockfile_path, "faketool", "2.0", digest)
    runner = ensure("faketool", base_env={})
    assert "faketool-2.0" in runner.env["PATH"]
    assert migrations == [("1.0", "2.0")]


def test_lazy_installs_disabled(pm_env, monkeypatch):
    import importlib

    ensure_mod = importlib.import_module("pm.ensure")
    monkeypatch.setattr(ensure_mod, "lazy_installs_allowed", lambda: False)
    with pytest.raises(InstallError, match="lazy installs are disabled"):
        ensure_mod.ensure("faketool", base_env={})


def test_missing_platform_is_declared(pm_env):
    from pm.ensure import ensure

    lockfile_path, *_ = pm_env
    pkg = registry._packages["faketool"]
    pkg.targets = {current_target(): "no artifact for this platform"}
    try:
        with pytest.raises(InstallError, match="no artifact"):
            ensure("faketool", base_env={})
    finally:
        pkg.targets = None


def test_corrupt_facts_degrades_to_empty(pm_env):
    from pm.ensure import ensure, is_installed

    _, runtime, *_ = pm_env
    ensure("faketool", base_env={})
    (runtime / "facts.json").write_text("{ not json", encoding="utf-8")
    assert not is_installed("faketool")


def test_concurrent_installs_do_not_clobber(pm_env):
    from pm.ensure import ensure, is_installed

    lockfile_path, _, docroot, _ = pm_env
    name, digest = make_tar(docroot, "deptool-1.0.tar.gz", {"bin/faketool": "y"})
    _pin(lockfile_path, "deptool", "1.0", digest)

    errors = []

    def run(target_name):
        try:
            ensure(target_name, base_env={})
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=run, args=("faketool",)),
        threading.Thread(target=run, args=("deptool",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert is_installed("faketool") and is_installed("deptool")


def test_single_flight_one_store_entry(pm_env):
    from pm.ensure import ensure

    _, runtime, *_ = pm_env
    errors = []

    def go():
        try:
            ensure("faketool", base_env={})
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    entries = [p for p in runtime.iterdir() if p.name.startswith("faketool-")]
    assert len(entries) == 1


def test_gc_keeps_used_removes_orphans(pm_env):
    from pm.cli import cmd_gc
    from pm.ensure import ensure

    _, runtime, *_ = pm_env
    ensure("faketool", base_env={})
    orphan = runtime / "orphan-9.9-nowhere"
    orphan.mkdir()
    cmd_gc(None)
    assert not orphan.exists()
    assert any(p.name.startswith("faketool-1.0") for p in runtime.iterdir())


def test_env_for_never_installs(pm_env):
    from pm.ensure import env_for

    _, runtime, *_ = pm_env
    env = env_for("faketool", base_env={})
    assert "faketool-1.0" not in env.get("PATH", "")
    installed = [p for p in runtime.iterdir() if p.is_dir()] if runtime.is_dir() else []
    assert not any(p.name.startswith("faketool-") for p in installed)


def test_facts_adopt_by_path_substitution(tmp_path):
    store_a = tmp_path / "bundle-store"
    facts_a = Facts(store_a / "facts.json")
    store_a.mkdir()
    facts_a.record("tool", "1.0", "tool-1.0-any", {"PATH": [str(store_a / "tool-1.0-any" / "bin")]}, store_a)

    raw = (store_a / "facts.json").read_text(encoding="utf-8")
    assert "{{store}}" in raw and str(store_a) not in raw

    store_b = tmp_path / "user-store"
    store_b.mkdir()
    (store_a / "facts.json").rename(store_b / "facts.json")
    facts_b = Facts(store_b / "facts.json")
    env = facts_b.env_for("tool", store_b)
    assert env["PATH"] == [str(store_b / "tool-1.0-any" / "bin")]


def test_internal_packages_refused_by_run(pm_env):
    from pm.ensure import run as pm_run

    registry._packages["faketool"].internal = True
    try:
        with pytest.raises(InstallError, match="internal package"):
            pm_run("faketool", ["--version"])
    finally:
        registry._packages["faketool"].internal = False


def test_compose_env_dependents_win_non_path_too():
    env = compose_env([{"X": "dep"}, {"X": "dependent"}], base={})
    assert env["X"] == "dependent"


def test_flatten_refuses_layout_dirs(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tool").write_text("x")
    flatten_single_dir(tmp_path)
    assert (tmp_path / "bin" / "tool").is_file()


class FakeVenv(StatePackage):
    name = "venv"

    def __init__(self):
        self.applied: list[list[str]] = []
        self.lock_content = b"lock-v1"

    def expected_stamp(self, extras):
        import hashlib

        h = hashlib.sha256(self.lock_content)
        h.update(",".join(sorted(extras)).encode())
        return h.hexdigest()

    def apply(self, extras):
        self.applied.append(list(extras))


@pytest.fixture
def venv_env(pm_env):
    fake = FakeVenv()
    registry._packages["venv"] = fake
    return pm_env, fake


def test_sync_venv_applies_once_then_stamps(venv_env):
    from pm.ensure import sync_venv

    _, fake = venv_env
    sync_venv()
    sync_venv()
    assert fake.applied == [[]]


def test_sync_venv_unions_extras(venv_env):
    from pm.ensure import sync_venv

    _, fake = venv_env
    sync_venv(["telegram"])
    sync_venv(["anthropic"])
    sync_venv(["telegram"])
    assert fake.applied == [["telegram"], ["anthropic", "telegram"]]


def test_check_reports_venv_drift_and_missing_tools(venv_env):
    from pm.ensure import check, ensure, sync_venv

    (pm_env_tuple, fake) = venv_env
    lockfile_path, runtime, *_ = pm_env_tuple

    assert check() == []  # pm never touched this install: silent

    ensure("faketool", base_env={})
    sync_venv()
    assert check() == []

    fake.lock_content = b"lock-v2"  # uv.lock changed underneath
    problems = check()
    assert problems == ["venv: out of sync with uv.lock"]

    _pin(lockfile_path, "faketool", "9.9", "0" * 64)  # tool outdated now
    problems = check()
    assert "faketool: not installed or outdated" in problems
