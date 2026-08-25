"""CLI self-update of the sealed desktop bundle (hermes_cli.sealed_update).

Network and process effects are isolated behind small functions; these
tests cover the decision logic (layout resolution, feed parsing, artifact
choice, version compare, sha512 verify) with real files and fake bytes —
no live network, no processes.
"""

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.sealed_update import (
    SealedUpdateUnavailable,
    channel_manifest_url,
    download_and_verify,
    parse_version,
    pick_artifact,
    read_feed_config,
    render_apply_script,
    resolve_app_layout,
)


def _mk_bundle(tmp_path: Path, with_feed=True, exe_name="Hermes.exe") -> Path:
    """A miniature HermesBundled layout; returns the payload repo root."""
    app = tmp_path / "HermesBundled"
    repo = app / "resources" / "agent-payload" / "repo"
    repo.mkdir(parents=True)
    if with_feed:
        (app / "resources" / "app-update.yml").write_text(
            "owner: ethernet8023\nrepo: hermes-agent\nprovider: github\nchannel: latest\n",
            encoding="utf-8",
        )
    if exe_name:
        (app / exe_name).write_bytes(b"MZ")
    (app / "Uninstall Hermes.exe").write_bytes(b"MZ")
    (repo / "install-stamp.json").write_text(
        json.dumps({"baseVersion": "0.27.0", "payload": "bundled"}), encoding="utf-8"
    )
    return repo


class TestLayoutAndFeed:
    def test_resolves_bundle_layout(self, tmp_path):
        repo = _mk_bundle(tmp_path)
        # A Windows bundle shape. The layout is pure path arithmetic, so
        # naming the platform resolves it from any host (the sealed
        # updater itself is Windows-only at runtime).
        layout = resolve_app_layout(repo, platform="win32")
        assert layout["app_root"] == tmp_path / "HermesBundled"
        assert layout["feed_file"].name == "app-update.yml"
        # The launcher, never the uninstaller.
        assert layout["exe"].name == "Hermes.exe"

    def test_non_bundle_sealed_tree_is_unavailable(self, tmp_path):
        # docker/nix shape: sealed repo with no app above it.
        repo = tmp_path / "opt" / "hermes" / "repo"
        repo.mkdir(parents=True)
        with pytest.raises(SealedUpdateUnavailable):
            resolve_app_layout(repo)

    def test_feed_requires_github_provider(self, tmp_path):
        repo = _mk_bundle(tmp_path)
        feed = repo.parent.parent / "app-update.yml"
        feed.write_text("provider: s3\nbucket: x\n", encoding="utf-8")
        with pytest.raises(SealedUpdateUnavailable):
            read_feed_config(feed)

    def test_manifest_url_shape(self, tmp_path):
        repo = _mk_bundle(tmp_path)
        cfg = read_feed_config(repo.parent.parent / "app-update.yml")
        assert channel_manifest_url(cfg) == (
            "https://github.com/ethernet8023/hermes-agent/releases/latest/download/latest.yml"
        )


class TestArtifactAndVersions:
    def test_single_file_manifest_needs_no_choice(self):
        entry = {"url": "HermesBundled-0.28.0-win-arm64.exe", "sha512": "x"}
        assert pick_artifact({"files": [entry]}) is entry

    def test_version_compare_semver_not_string(self):
        assert parse_version("0.10.0") > parse_version("0.9.9")
        assert parse_version("0.27.0") == parse_version("v0.27.0")


class TestDownloadVerify:
    def _serve(self, payload: bytes):
        class _Resp:
            def __init__(self):
                self._chunks = [payload, b""]

            def read(self, _n):
                return self._chunks.pop(0)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    def test_good_hash_lands_file(self, tmp_path):
        payload = b"new installer bytes"
        sha = base64.b64encode(hashlib.sha512(payload).digest()).decode()
        artifact = {"url": "HermesBundled-0.28.0-win-arm64.exe", "sha512": sha}
        cfg = {"owner": "o", "repo": "r"}
        with patch("urllib.request.urlopen", return_value=self._serve(payload)):
            dest = download_and_verify(cfg, artifact, tmp_path)
        assert dest.read_bytes() == payload

    def test_bad_hash_deletes_and_raises(self, tmp_path):
        artifact = {"url": "HermesBundled-0.28.0-win-arm64.exe", "sha512": "bogus"}
        cfg = {"owner": "o", "repo": "r"}
        with patch("urllib.request.urlopen", return_value=self._serve(b"evil")):
            with pytest.raises(SealedUpdateUnavailable):
                download_and_verify(cfg, artifact, tmp_path)
        assert not (tmp_path / artifact["url"]).exists()

    def test_weird_artifact_name_rejected_before_any_io(self, tmp_path):
        artifact = {"url": "../../escape.exe", "sha512": "x"}
        with pytest.raises(SealedUpdateUnavailable):
            download_and_verify({"owner": "o", "repo": "r"}, artifact, tmp_path)


class TestApplyScript:
    def test_kills_by_app_root_and_conditionally_relaunches(self, tmp_path):
        app = tmp_path / "HermesBundled"
        script = render_apply_script(app, tmp_path / "new.exe", app / "Hermes.exe")
        assert str(app) in script
        assert "StartsWith($appRoot" in script
        assert "'/S'" in script
        assert "$guiWasRunning" in script and "Start-Process -FilePath" in script

    def test_headless_bundle_never_relaunches(self, tmp_path):
        script = render_apply_script(tmp_path, tmp_path / "new.exe", exe=None)
        assert "if ($guiWasRunning)" not in script


class TestUpdateEntryFallback:
    """cmd_update: a win32 electron-updater tree tries self-update first;
    anything the path cannot serve falls back to the steward refusal
    (exit 1). The gate is the stamp's updateMechanism — no manifest."""

    def _args(self, **kw):
        base = dict(check=False, gateway=False, branch=None, channel=None,
                    set_channel=None, install_id=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_unavailable_falls_back_to_refusal(self, capsys):
        from hermes_cli.main import cmd_update

        with (
            patch("hermes_cli.config.detect_install_method", return_value="desktop-app"),
            patch(
                "installation.tree.read_build_info",
                return_value={"updateMechanism": "electron-updater"},
            ),
            patch(
                "hermes_cli.sealed_update.cmd_update_sealed_desktop",
                side_effect=SealedUpdateUnavailable("not windows"),
            ),
            patch("hermes_cli.main._cmd_update_impl") as impl,
        ):
            with pytest.raises(SystemExit) as exc:
                cmd_update(self._args())
        assert exc.value.code == 1
        impl.assert_not_called()

    def test_successful_self_update_exits_zero_without_refusal(self, capsys):
        from hermes_cli.main import cmd_update

        with (
            patch("hermes_cli.config.detect_install_method", return_value="desktop-app"),
            patch(
                "installation.tree.read_build_info",
                return_value={"updateMechanism": "electron-updater"},
            ),
            patch("hermes_cli.sealed_update.cmd_update_sealed_desktop", return_value=0),
            patch("hermes_cli.main._cmd_update_impl") as impl,
        ):
            with pytest.raises(SystemExit) as exc:
                cmd_update(self._args())
        assert exc.value.code == 0
        impl.assert_not_called()
