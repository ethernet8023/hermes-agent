"""The stamp's required ``updateMechanism`` field — writer and readers.

Writer: scripts/write_install_stamp.py refuses to build a stamp without a
valid mechanism. Readers (installation.tree.read_build_info and its
stdlib-only mirrors) HARD-FAIL a stamp missing the field: a mechanism-less
stamp is a build-lane bug, and guessing would misroute updates for every
install of that artifact.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from write_install_stamp import UPDATE_MECHANISMS, build_stamp  # noqa: E402


class TestWriter:
    @pytest.mark.parametrize("mechanism", UPDATE_MECHANISMS)
    def test_valid_mechanisms_are_emitted(self, mechanism):
        stamp = build_stamp(
            commit="a" * 40, source="ci", update_mechanism=mechanism
        )
        assert stamp["updateMechanism"] == mechanism

    def test_invalid_mechanism_refused(self):
        with pytest.raises(SystemExit):
            build_stamp(commit="a" * 40, update_mechanism="carrier-pigeon")

    def test_mechanism_is_required_by_the_cli(self, tmp_path):
        """argparse enforces --update-mechanism; a lane that forgets it dies."""
        out = tmp_path / "install-stamp.json"
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "write_install_stamp.py"),
                "--output", str(out),
                "--commit", "b" * 40,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert "--update-mechanism" in result.stderr
        assert not out.exists()

    def test_cli_emits_the_field(self, tmp_path):
        out = tmp_path / "install-stamp.json"
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "write_install_stamp.py"),
                "--output", str(out),
                "--commit", "c" * 40,
                "--distribution", "docker",
                "--update-mechanism", "external",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(out.read_text())["updateMechanism"] == "external"


class TestReaderHardFail:
    def _write(self, root, fields):
        (root / "install-stamp.json").write_text(
            json.dumps({"schemaVersion": 2, "commit": "a" * 40, **fields}),
            encoding="utf-8",
        )

    def test_read_build_info_rejects_missing_mechanism(self, tmp_path):
        from installation.tree import read_build_info

        self._write(tmp_path, {"distribution": "docker"})
        with pytest.raises(RuntimeError, match="updateMechanism"):
            read_build_info(tmp_path)

    def test_read_build_info_rejects_bogus_mechanism(self, tmp_path):
        from installation.tree import read_build_info

        self._write(tmp_path, {"distribution": "docker", "updateMechanism": "yolo"})
        with pytest.raises(RuntimeError, match="updateMechanism"):
            read_build_info(tmp_path)

    @pytest.mark.parametrize("mechanism", ["self", "electron-updater", "external"])
    def test_read_build_info_accepts_valid(self, tmp_path, mechanism):
        from installation.tree import read_build_info

        self._write(tmp_path, {"distribution": "nix", "updateMechanism": mechanism})
        assert read_build_info(tmp_path)["updateMechanism"] == mechanism

    def test_read_build_info_tolerates_a_bom(self, tmp_path):
        """Windows tooling BOM-prefixes files it touches (PR #3 live repro):
        the reader must not demote a BOM'd stamp to {}."""
        from installation.tree import read_build_info

        raw = json.dumps(
            {"commit": "a" * 40, "distribution": "desktop-app",
             "updateMechanism": "electron-updater"}
        )
        (tmp_path / "install-stamp.json").write_bytes(b"\xef\xbb\xbf" + raw.encode("utf-8"))
        assert read_build_info(tmp_path)["updateMechanism"] == "electron-updater"

    def test_missing_stamp_is_still_empty_dict(self, tmp_path):
        from installation.tree import read_build_info

        assert read_build_info(tmp_path) == {}

    def test_venv_sync_is_sealed_rejects_missing_mechanism(self, tmp_path):
        from hermes_cli.venv_sync import _is_sealed

        self._write(tmp_path, {"payload": "full"})
        with pytest.raises(RuntimeError, match="updateMechanism"):
            _is_sealed(tmp_path)

    def test_startup_fast_rejects_missing_mechanism(self, tmp_path, monkeypatch):
        from hermes_cli import _startup_fast

        self._write(tmp_path, {"distribution": "docker"})
        monkeypatch.setattr(_startup_fast, "project_root_str", lambda: str(tmp_path))
        with pytest.raises(RuntimeError, match="updateMechanism"):
            _startup_fast.read_install_method()

    def test_version_info_rejects_missing_mechanism(self, tmp_path, monkeypatch):
        from hermes_cli import version_info

        stamp_file = tmp_path / "install-stamp.json"
        stamp_file.write_text(
            json.dumps({"commit": "d" * 40, "source": "ci", "distribution": "docker"})
        )
        monkeypatch.setattr(version_info, "_resolve_stamp_file", lambda: stamp_file)
        with pytest.raises(RuntimeError, match="updateMechanism"):
            version_info._stamp_version_info()


class TestLaneValues:
    """Each build lane bakes the mechanism its artifact actually updates by.

    The lanes are shell/CI/nix surfaces; these tests pin the flag value at
    the source text level the way the arch-gate lockstep test does — the
    invocation and the expected mechanism must agree in both directions.
    """

    def _lane(self, relpath):
        return (REPO_ROOT / relpath).read_text(encoding="utf-8")

    def test_docker_lane_is_external(self):
        workflow = self._lane(".github/workflows/docker.yml")
        for line in workflow.splitlines():
            if "write_install_stamp.py" in line:
                assert "--update-mechanism external" in line, line

    def test_nix_desktop_lane_is_external(self):
        lane = self._lane("nix/desktop.nix")
        assert "--update-mechanism external" in lane

    def test_nix_agent_stamp_is_external(self):
        lane = self._lane("nix/hermes-agent.nix")
        assert '"updateMechanism":"external"' in lane

    def test_desktop_payload_lane_is_electron_updater(self):
        """The staging lane defaults to electron-updater and takes the
        per-pass override through HERMES_PAYLOAD_UPDATE_MECHANISM (the
        win32 msix pass rebuilds top-down with `external`)."""
        lane = self._lane("apps/desktop/scripts/stage-agent-payloads.mjs")
        assert (
            '"--update-mechanism", process.env.HERMES_PAYLOAD_UPDATE_MECHANISM || "electron-updater"'
            in lane
        )

    def test_desktop_dev_build_is_electron_updater(self):
        """The build script stamps through write-shell-stamp.mjs, whose
        default (env knob unset — every dev build) is electron-updater."""
        lane = self._lane("apps/desktop/package.json")
        build = json.loads(lane)["scripts"]["build"]
        assert "write-shell-stamp.mjs" in build
        writer = self._lane("apps/desktop/scripts/write-shell-stamp.mjs")
        assert (
            "process.env.HERMES_PAYLOAD_UPDATE_MECHANISM || 'electron-updater'" in writer
        )

    def test_installer_engines_stamp_self(self):
        sh = self._lane("scripts/install.sh")
        ps1 = self._lane("scripts/install.ps1")
        # install.sh writes the stamp via single-quoted printf.
        assert '"updateMechanism": "self"' in sh
        # install.ps1 writes via Set-Content (a BOM is fine — every stamp
        # reader reads utf-8-sig; see test_read_build_info_tolerates_a_bom).
        assert '`"updateMechanism`": `"self`"' in ps1
