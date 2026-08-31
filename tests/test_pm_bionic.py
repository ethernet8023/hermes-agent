"""Behavior contracts for pm's bionic target + DebPackage + stage_only.

The lock VALUES are bumped by review; these tests pin the relationships:
the linux-arm64-bionic rows exist and agree with their suppliers' shapes,
DebPackage extraction is hardened, and cross-target staging never touches
this host's installed facts.
"""

from __future__ import annotations

import json
import tarfile
import io
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pm():
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    import pm

    return pm


@pytest.fixture(scope="module")
def lock():
    return json.loads((REPO_ROOT / "pm" / "lock.json").read_text(encoding="utf-8"))


def test_all_targets_includes_bionic():
    from pm.store import ALL_TARGETS

    assert "linux-arm64-bionic" in ALL_TARGETS
    assert ALL_TARGETS.count("linux-arm64-bionic") == 1


def test_python_bionic_row_matches_supplier(lock):
    """The python bionic row must agree with the lock version key and the
    TUR .deb naming convention (python3.11_<version>_aarch64.deb)."""
    row = lock["packages"]["python"]["artifacts"].get("linux-arm64-bionic")
    assert row, "python has no linux-arm64-bionic artifact"
    version = lock["packages"]["python"]["version"].partition("+")[0]
    assert row["url"] == f"https://tur.kcubeterm.com/pool/tur/python3.11_{version}_aarch64.deb"
    assert len(row["sha256"]) == 64


def test_node_bionic_row_matches_supplier(lock):
    """The node bionic row must agree with the lock version key and the
    termux nodejs .deb naming convention (nodejs_<version>-1)."""
    row = lock["packages"]["node"]["artifacts"].get("linux-arm64-bionic")
    assert row, "node has no linux-arm64-bionic artifact"
    version = lock["packages"]["node"]["version"]
    assert row["url"].endswith(f"/nodejs/nodejs_{version}-1_aarch64.deb")
    assert len(row["sha256"]) == 64


def test_termux_docker_row_pins_digest(lock):
    row = lock["packages"]["termux-docker"]["artifacts"]["linux-arm64-bionic"]
    version = lock["packages"]["termux-docker"]["version"]
    assert version.startswith("sha256:")
    assert len(version) == 7 + 64
    assert row["url"] == f"docker://termux/termux-docker@{version}"


def test_bionic_fetch_urls_resolve():
    from pm.registry import get_package

    py = get_package("python")
    version = "3.11.15+20260807"
    assert py.fetch_url(version, "linux-arm64-bionic").endswith(
        "python3.11_3.11.15_aarch64.deb"
    )
    nd = get_package("node")
    assert nd.fetch_url("26.4.0", "linux-arm64-bionic").endswith(
        "nodejs_26.4.0-1_aarch64.deb"
    )


def _build_fake_deb(path: Path, control: dict[str, str], files: dict[str, bytes]) -> None:
    def ar_member(name: str, data: bytes) -> bytes:
        hdr = (
            name.ljust(16).encode()
            + b"0".ljust(12)
            + b"0".ljust(6)
            + b"0".ljust(6)
            + b"100644".ljust(8)
            + str(len(data)).encode().ljust(10)
            + b"`\n"
        )
        pad = b"\n" if len(data) % 2 else b""
        return hdr + data + pad

    ctrl_buf = io.BytesIO()
    with tarfile.open(fileobj=ctrl_buf, mode="w:gz") as tf:
        body = "".join(f"{k}: {v}\n" for k, v in control.items()).encode()
        info = tarfile.TarInfo("control")
        info.size = len(body)
        tf.addfile(info, io.BytesIO(body))
    data_buf = io.BytesIO()
    with tarfile.open(fileobj=data_buf, mode="w:gz") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    path.write_bytes(
        b"!<arch>\n"
        + ar_member("debian-binary", b"2.0\n")
        + ar_member("control.tar.gz", ctrl_buf.getvalue())
        + ar_member("data.tar.gz", data_buf.getvalue())
    )


def test_debpackage_unpack_hardened(tmp_path: Path):
    """DebPackage.unpack extracts data members and refuses traversal."""
    from pm.package import DebPackage

    class _P(DebPackage):
        name = "test-deb"

    deb = tmp_path / "test.deb"
    _build_fake_deb(
        deb,
        {"Package": "test-deb", "Version": "1.0"},
        {"data/data/com.termux/files/usr/bin/tool": b"\x7fELF"},
    )
    staged = tmp_path / "staged"
    staged.mkdir()
    _P().unpack(deb, staged, "linux-arm64-bionic")
    assert (staged / "data/data/com.termux/files/usr/bin/tool").read_bytes() == b"\x7fELF"

    # traversal member must be refused
    evil = tmp_path / "evil.deb"
    _build_fake_deb(
        evil, {"Package": "evil", "Version": "1.0"}, {"../escape": b"x"}
    )
    with pytest.raises(Exception):
        _P().unpack(evil, tmp_path / "staged2", "linux-arm64-bionic")


def test_python_bionic_verify_is_file_evidence(tmp_path: Path):
    """bionic verify never executes the staged binary; presence is the
    contract (the digest already proved the bytes)."""
    from pm.registry import get_package

    py = get_package("python")
    bin_rel = Path("data/data/com.termux/files/usr/bin/python3.11")
    entry = tmp_path / "entry"
    (entry / bin_rel).parent.mkdir(parents=True)
    (entry / bin_rel).write_bytes(b"bionic-elf-bytes")
    assert py.verify(entry, "linux-arm64-bionic") == ""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "missing" in py.verify(empty, "linux-arm64-bionic")


def test_stage_only_does_not_record_host_facts(tmp_path, monkeypatch):
    """stage_only publishes the entry but must not touch this machine's
    installed facts -- the fact slot belongs to the HOST target."""
    pm = _pm()
    from pm.ensure import stage_only
    from pm.lock import Facts
    from pm.paths import facts_path

    from pm.lock import Facts

    def snapshot() -> dict:
        path = facts_path()
        if not path.is_file():
            return {}
        return Facts(path)._packages

    before = snapshot()
    entry = stage_only("termux-docker", "linux-arm64-bionic")
    after = snapshot()
    assert before == after
    # termux-docker stages nothing; the entry path still resolves
    assert "termux-docker" in str(entry) or entry is not None
