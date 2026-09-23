"""Version stamping writes a build tree, never the checkout it was invoked from.

A release used to commit the bumped version back onto the branch. The version
now lives in the ref, so stamping is a build step: it rewrites a copy of the
tree and returns the paths it touched, and the caller's tree is unchanged.
"""
import importlib.util
import json
from pathlib import Path

import pytest


def _tree(root: Path) -> None:
    (root / "hermes_cli").mkdir()
    (root / "hermes_cli" / "__init__.py").write_text(
        '__release_date__ = "2026.1.1"\n', encoding="utf-8")
    (root / "pyproject.toml").write_text('version = "0.0.0"\n', encoding="utf-8")
    desktop = root / "apps" / "desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text('{"version": "0.0.0"}\n', encoding="utf-8")
    (root / "package-lock.json").write_text(
        '{"version": "0.0.0", "packages": {'
        '"apps/desktop": {"name": "hermes", "version": "0.0.0"}, '
        '"apps/bootstrap-installer": {"name": "@hermes/bootstrap-installer", "version": "0.0.0"}}}\n',
        encoding="utf-8")
    (root / "uv.lock").write_text(
        '[[package]]\nname = "hermes-agent"\nversion = "0.0.0"\n'
        '[[package]]\nname = "other"\nversion = "0.0.0"\n', encoding="utf-8")
    (root / "nix").mkdir()
    (root / "nix" / "hermes-agent.nix").write_text(
        '{\n  version ? "0.0.0",\n}: version\n', encoding="utf-8")
    installer = root / "apps" / "bootstrap-installer" / "src-tauri"
    installer.mkdir(parents=True)
    (root / "apps" / "bootstrap-installer" / "package.json").write_text(
        '{"name": "x", "version": "0.0.0"}\n', encoding="utf-8")
    (installer / "tauri.conf.json").write_text(
        '{"productName": "Hermes", "version": "0.0.0"}\n', encoding="utf-8")
    (installer / "Cargo.toml").write_text('[package]\nversion = "0.0.0"\n', encoding="utf-8")
    (installer / "Cargo.lock").write_text(
        '[[package]]\nname = "bootstrap-installer"\nversion = "0.21.1"\n', encoding="utf-8")


def test_stamping_writes_the_build_tree_and_leaves_the_source_tree(tmp_path, monkeypatch):
    from scripts.releases.stamping import stamp

    source = tmp_path / "source"
    build = tmp_path / "build"
    source.mkdir()
    build.mkdir()
    _tree(source)
    _tree(build)
    before = (source / "pyproject.toml").read_text(encoding="utf-8")

    written = stamp(build, "0.21.5", "2026.9.22")

    assert (source / "pyproject.toml").read_text(encoding="utf-8") == before
    assert 'version = "0.21.5"' in (build / "pyproject.toml").read_text(encoding="utf-8")
    spec = importlib.util.spec_from_file_location("stamped_version", build / "hermes_cli" / "_version.py")
    assert spec is not None and spec.loader is not None
    stamped_version = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stamped_version)
    assert stamped_version.__version__ == "0.21.5"
    assert json.loads((build / "apps" / "desktop" / "package.json").read_text())["version"] == "0.21.5"
    # The lockfile root stays; stamped workspace mirrors move with their manifests.
    lock = json.loads((build / "package-lock.json").read_text())
    assert lock["version"] == "0.0.0"
    assert lock["packages"]["apps/desktop"]["version"] == "0.21.5"
    assert lock["packages"]["apps/bootstrap-installer"]["version"] == "0.21.5"
    # uv.lock records the root package once, and no other package moves with it.
    uv = (build / "uv.lock").read_text(encoding="utf-8")
    assert uv.count('version = "0.21.5"') == 1
    assert 'name = "hermes-agent"\nversion = "0.21.5"' in uv
    assert 'version ? "0.21.5"' in (build / "nix" / "hermes-agent.nix").read_text()
    cargo_lock = (build / "apps" / "bootstrap-installer" / "src-tauri" / "Cargo.lock").read_text()
    assert 'version = "0.21.5"' in cargo_lock
    from scripts import write_install_stamp
    monkeypatch.setattr(write_install_stamp, "_REPO_ROOT", build)
    assert write_install_stamp._parse_release_metadata() == ("0.21.5", "2026.9.22")
    stamp_path = build / "install-stamp.json"
    stamp = write_install_stamp.write_stamp(
        stamp_path, update_mechanism="external", distribution="docker",
        commit="a" * 40, branch="main", dirty=False, commit_date=1, distance=0,
    )
    assert stamp["baseVersion"] == "0.21.5"
    assert json.loads(stamp_path.read_text(encoding="utf-8"))["baseVersion"] == "0.21.5"
    assert all(path.is_relative_to(build) for path in written)

    tauri = build / "apps" / "bootstrap-installer" / "src-tauri" / "tauri.conf.json"
    tauri.write_text('{"version": "0.0.0"}\n', encoding="utf-8")
    from scripts.releases.stamping import validate_bootstrap_version
    with pytest.raises(ValueError, match="Tauri config"):
        validate_bootstrap_version(build, "0.21.5")


def test_stamping_a_payload_snapshot_without_apps_stamps_the_runtime(tmp_path):
    """The bundled payload snapshot drops apps/ (INERT_SNAPSHOT_DIRS), so it has no installer to validate."""
    import shutil

    from scripts.releases.stamping import stamp

    _tree(tmp_path)
    shutil.rmtree(tmp_path / "apps")

    stamp(tmp_path, "0.21.5", "2026.9.22")

    assert 'version = "0.21.5"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
