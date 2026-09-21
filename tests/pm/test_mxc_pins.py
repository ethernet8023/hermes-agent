"""The MXC kit and the sandbox shell are pinned pm packages.

The kit is a branch archive with no release index, so its pin is a hand edit.
busybox has a directory listing, so its pin follows the newest build both
Windows arches serve, and the hook hashes the exes itself because frippery.org
publishes no checksums.
"""

import hashlib

import pytest

from pm import Lockfile, get_package, paths
from pm.store import ALL_TARGETS


KIT_ARCHIVE = ("https://github.com/jquesnelle/mxc-kit-binaries/archive/refs/heads/"
               "ca7ea12a-arm64.zip")
KIT_SHA256 = "275ba11a63fe3f00e1b8a13fcedda61223c1422e32d0bba3cfbc6ed840574a6f"

BUSYBOX_SHA256 = {
    "win32-arm64": "e67f873d19d58c535cc9f0c4965ffd622e19b7bab87e3da89cb2185fb54464d7",
    "win32-x64": "6e263d154d8548d1eb936f65d1d8312c80df31c45974e48d6335e4dcc0f4f34c",
}


def test_mxc_kit_pins_the_branch_archive_for_arm64_and_gaps_everywhere_else():
    package = get_package("mxc-kit")
    lock = Lockfile(paths.lockfile_path())
    version = lock.version("mxc-kit")

    assert version is not None
    assert package.fetch_url(version, "win32-arm64") == KIT_ARCHIVE
    assert package.binary_rel["win32"] == "bin/wxc-exec.exe"
    assert package.on_path is False and package.probe_version is False
    assert package.latest_versions("win32-arm64") == []

    pinned = lock.artifacts("mxc-kit", "win32-arm64")[0]
    assert pinned["url"] == KIT_ARCHIVE
    assert pinned["sha256"] == KIT_SHA256

    for target in ALL_TARGETS:
        if target == "win32-arm64":
            continue
        assert package.missing_reason(target), target


def test_busybox_pins_both_windows_arches_and_copies_the_exe(tmp_path):
    package = get_package("busybox")
    lock = Lockfile(paths.lockfile_path())
    version = lock.version("busybox")

    assert version == "FRP-6075-g169694ebd"
    assert package.on_path is False and package.probe_version is False
    for target, digest in BUSYBOX_SHA256.items():
        assert package.fetch_url(version, target).endswith(f"busybox-w64{'a' if 'arm64' in target else 'u'}-{version}.exe")
        assert lock.artifacts("busybox", target)[0]["sha256"] == digest

    for target in ALL_TARGETS:
        if target.startswith("win32"):
            continue
        assert package.missing_reason(target), target

    archive = tmp_path / "busybox.exe"
    archive.write_bytes(b"busybox-bytes")
    staged = tmp_path / "staged"
    package.unpack(archive, staged, "win32-arm64")
    assert (staged / "busybox-sh.exe").read_bytes() == b"busybox-bytes"
    assert package.binary(staged, "win32-arm64") == staged / "busybox-sh.exe"


def test_busybox_update_pins_the_newest_build_both_arches_serve(monkeypatch, tmp_path):
    package = get_package("busybox")
    listing = ("busybox-w64a-FRP-5857-g3681e397f.exe\n"
               "busybox-w64u-FRP-5857-g3681e397f.exe\n"
               "busybox-w64a-FRP-6075-g169694ebd.exe\n"
               "busybox-w64u-FRP-6075-g169694ebd.exe\n"
               "busybox-w64a-FRP-9999-gone.exe\n")  # arm64 only: must not win
    monkeypatch.setattr(package, "_listing", lambda: listing)

    fetched = {}

    def fake_fetch(url, dest):
        fetched[url] = b"bytes-for-" + url.encode()
        dest.write_bytes(fetched[url])

    monkeypatch.setattr(package, "_fetch", fake_fetch)

    assert package.latest_versions("win32-arm64") == ["FRP-6075-g169694ebd", "FRP-5857-g3681e397f"]

    version = package.latest_versions("win32-x64")[0]
    digests = {}
    for target in ("win32-arm64", "win32-x64"):
        url = package.fetch_url(version, target)
        dest = tmp_path / target
        package._fetch(url, dest)
        digests[target] = hashlib.sha256(dest.read_bytes()).hexdigest()
    assert set(fetched) == {package.fetch_url(version, t) for t in ("win32-arm64", "win32-x64")}
    assert all(len(d) == 64 for d in digests.values())
