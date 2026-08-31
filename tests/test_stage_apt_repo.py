"""Tests for scripts/termux/stage_apt_repo.py — stdlib + pytest, no network, no gpg."""

import gzip
import io
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts" / "termux"
sys.path.insert(0, str(SCRIPTS))

import stage_apt_repo  # noqa: E402


def make_deb(path: Path, package: str, version: str, arch: str = "arm64") -> None:
    """Build a minimal .deb (ar archive with control.tar.gz) using stdlib only."""
    control = (
        f"Package: {package}\n"
        f"Version: {version}\n"
        f"Architecture: {arch}\n"
        f"Maintainer: Test <test@example.com>\n"
        f"Description: test package {package}\n"
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = control.encode("utf-8")
        ti = tarfile.TarInfo("control")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))

    ar = io.BytesIO()
    ar.write(b"!<arch>\n")
    payload = buf.getvalue()
    header = "{:<16}{:<12}{:<6}{:<6}{:<8}{:<10}".format(
        "control.tar.gz", "0", "0", "0", "100644", str(len(payload))
    ).encode() + b"`\n"
    ar.write(header)
    ar.write(payload)
    if len(payload) % 2:
        ar.write(b"\n")
    path.write_bytes(ar.getvalue())


@pytest.fixture
def no_gpg(monkeypatch):
    """Make the script believe gpg is absent so signing is skipped (exit 3)."""
    monkeypatch.setattr(stage_apt_repo.shutil, "which", lambda _: None)


@pytest.fixture
def fake_gpg(monkeypatch, tmp_path):
    """Make the script believe gpg is present, but stub out signing."""
    monkeypatch.setattr(stage_apt_repo.shutil, "which", lambda _: "C:/fake/gpg.exe")
    monkeypatch.setattr(stage_apt_repo, "sign", lambda *a, **k: None)
    key = tmp_path / "signing.asc"
    key.write_text("stub-key\n")
    return key


def test_control_field_extraction(tmp_path):
    deb = tmp_path / "pkg_a.deb"
    make_deb(deb, "hermes-agent", "1.2.3-1")
    fields = stage_apt_repo.deb_control_fields(deb)
    assert fields["Package"] == "hermes-agent"
    assert fields["Version"] == "1.2.3-1"
    assert fields["Architecture"] == "arm64"


def test_nightly_versions_below_stable():
    versions = ["1.2.3-1", "1.2.3~nightly.20260831120000-1", "1.2.4~nightly.1-1", "1.2.4-1"]
    ordered = sorted(versions, key=stage_apt_repo.deb_version_key)
    assert ordered == [
        "1.2.3~nightly.20260831120000-1",
        "1.2.3-1",
        "1.2.4~nightly.1-1",
        "1.2.4-1",
    ]


def test_dists_layout_and_pool_copy(tmp_path, fake_gpg):
    pool = tmp_path / "pool-in"
    pool.mkdir()
    make_deb(pool / "hermes-agent_1.2.3-1_arm64.deb", "hermes-agent", "1.2.3-1")
    out = tmp_path / "repo"
    r = stage_apt_repo.main(
        [
            "--pool", str(pool), "--out", str(out), "--suite", "hermes-stable",
            "--gpg-key-file", str(fake_gpg),
        ]
    )
    assert r == 0

    dists = out / "dists" / "hermes-stable" / "main" / "binary-arm64"
    assert (dists / "Packages").exists()
    assert (dists / "Packages.gz").exists()
    assert (out / "dists" / "hermes-stable" / "Release").exists()

    deb_out = out / "pool" / "h" / "hermes-agent_1.2.3-1_arm64.deb"
    assert deb_out.exists()

    text = (dists / "Packages").read_text(encoding="utf-8")
    assert "Package: hermes-agent" in text
    assert "Version: 1.2.3-1" in text
    assert "Filename: pool/h/hermes-agent_1.2.3-1_arm64.deb" in text
    assert "SHA256: " in text

    gz_text = gzip.decompress((dists / "Packages.gz").read_bytes()).decode()
    assert gz_text == text

    release = (out / "dists" / "hermes-stable" / "Release").read_text()
    assert "Suite: hermes-stable" in release
    assert "SHA256:" in release
    assert "SHA512:" in release


def test_immutability_refusal(tmp_path, fake_gpg, capsys):
    pool = tmp_path / "pool-in"
    pool.mkdir()
    make_deb(pool / "hermes-agent_1.2.3-1_arm64.deb", "hermes-agent", "1.2.3-1")
    out = tmp_path / "repo"
    assert stage_apt_repo.main(
        [
            "--pool", str(pool), "--out", str(out), "--suite", "hermes-stable",
            "--gpg-key-file", str(fake_gpg),
        ]
    ) == 0
    with pytest.raises(SystemExit) as ei:
        stage_apt_repo.main(
            [
                "--pool", str(pool), "--out", str(out), "--suite", "hermes-stable",
                "--gpg-key-file", str(fake_gpg),
            ]
        )
    assert ei.value.code == 2
    assert "already published" in capsys.readouterr().err


def test_unsigned_release_exit_3_without_gpg(tmp_path, no_gpg):
    pool = tmp_path / "pool-in"
    pool.mkdir()
    make_deb(pool / "hermes-agent_1.2.3-1_arm64.deb", "hermes-agent", "1.2.3-1")
    out = tmp_path / "repo"
    code = stage_apt_repo.main(
        ["--pool", str(pool), "--out", str(out), "--suite", "hermes-nightly"]
    )
    assert code == 3
    assert (out / "dists" / "hermes-nightly" / "Release").exists()
    assert not (out / "dists" / "hermes-nightly" / "InRelease").exists()
    assert not (out / "dists" / "hermes-nightly" / "Release.gpg").exists()


def test_signing_invoked_when_gpg_and_key_present(tmp_path, monkeypatch):
    """No real gpg: assert sign() is called with the right dists dir/key file."""
    calls = []

    def fake_sign(dists, release_path, gpg_key_file):
        calls.append((str(dists), str(release_path), str(gpg_key_file)))
        (dists / "InRelease").write_text("stub", encoding="utf-8")
        (dists / "Release.gpg").write_text("stub", encoding="utf-8")

    monkeypatch.setattr(stage_apt_repo.shutil, "which", lambda _: "C:/fake/gpg.exe")
    monkeypatch.setattr(stage_apt_repo, "sign", fake_sign)

    pool = tmp_path / "pool-in"
    pool.mkdir()
    make_deb(pool / "hermes-agent_1.2.3-1_arm64.deb", "hermes-agent", "1.2.3-1")
    out = tmp_path / "repo"
    keyfile = tmp_path / "signing.asc"
    keyfile.write_text("-----BEGIN PGP PRIVATE KEY BLOCK-----\n")
    code = stage_apt_repo.main(
        [
            "--pool", str(pool), "--out", str(out), "--suite", "hermes-stable",
            "--gpg-key-file", str(keyfile),
        ]
    )
    assert code == 0
    assert len(calls) == 1
    dists, release_path, kf = calls[0]
    assert dists == str(out / "dists" / "hermes-stable")
    assert release_path == str(out / "dists" / "hermes-stable" / "Release")
    assert kf == str(keyfile)
    assert (out / "dists" / "hermes-stable" / "InRelease").exists()
