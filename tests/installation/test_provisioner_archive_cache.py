"""The provisioner's pin-archive cache: a transport shortcut, never a trust one.

CI passes ``--archive-cache <dir>`` so a payload build that runs twice
against the same pin table downloads each artifact once. The invariant
under test everywhere here: the sha256 check is UNCONDITIONAL — a cache
hit is re-hashed exactly like a download, and an entry whose name lies
about its bytes is deleted, not trusted.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest import mock

import pytest

from installation.provisioner import _fetch_verified, main, prune_archive_cache
from installation.registry import PinnedFile

PAYLOAD = b"the pinned artifact bytes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
URL = "https://example.invalid/tool-1.0.0-darwin-arm64.tar.gz"
FILENAME = URL.rsplit("/", 1)[-1]


@pytest.fixture
def pin() -> PinnedFile:
    return PinnedFile(version="1.0.0", url=URL, sha256=DIGEST)


def _fake_download(payload: bytes = PAYLOAD):
    """A stand-in for _download that writes *payload* to dest."""

    def download(url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)

    return mock.Mock(side_effect=download)


def test_archive_hit_skips_download(tmp_path: Path, pin: PinnedFile) -> None:
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    (archive_dir / f"{DIGEST}-{FILENAME}").write_bytes(PAYLOAD)
    scratch = tmp_path / "scratch"

    download = _fake_download()
    with mock.patch("installation.provisioner._download", download):
        result = _fetch_verified(pin, scratch, archive_dir=archive_dir)

    download.assert_not_called()
    assert result == scratch / FILENAME
    assert result.read_bytes() == PAYLOAD


def test_archive_entry_with_wrong_bytes_is_deleted_and_redownloaded(
    tmp_path: Path, pin: PinnedFile
) -> None:
    """A cache entry whose NAME promises bytes it does not hold is junk."""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    liar = archive_dir / f"{DIGEST}-{FILENAME}"
    liar.write_bytes(b"not the pinned bytes at all")
    scratch = tmp_path / "scratch"

    download = _fake_download()
    with mock.patch("installation.provisioner._download", download):
        result = _fetch_verified(pin, scratch, archive_dir=archive_dir)

    download.assert_called_once()
    assert result == scratch / FILENAME
    assert result.read_bytes() == PAYLOAD
    # The lying entry was replaced by the verified download's write-through.
    assert liar.read_bytes() == PAYLOAD


def test_miss_downloads_and_writes_through(tmp_path: Path, pin: PinnedFile) -> None:
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    scratch = tmp_path / "scratch"

    download = _fake_download()
    with mock.patch("installation.provisioner._download", download):
        result = _fetch_verified(pin, scratch, archive_dir=archive_dir)

    download.assert_called_once()
    # The caller still gets the SCRATCH copy — the publish flow is untouched.
    assert result == scratch / FILENAME
    assert result.read_bytes() == PAYLOAD
    entry = archive_dir / f"{DIGEST}-{FILENAME}"
    assert entry.is_file()
    assert entry.read_bytes() == PAYLOAD
    # Write-through is atomic (tmp + os.replace): no half-written leftovers.
    assert sorted(p.name for p in archive_dir.iterdir()) == [entry.name]


def test_no_archive_dir_is_identical_to_today(tmp_path: Path, pin: PinnedFile) -> None:
    scratch = tmp_path / "scratch"

    download = _fake_download()
    with mock.patch("installation.provisioner._download", download):
        result = _fetch_verified(pin, scratch)

    download.assert_called_once_with(pin.url, scratch / FILENAME)
    assert result == scratch / FILENAME
    # Nothing beyond the scratch archive appears anywhere.
    assert sorted(p.name for p in scratch.iterdir()) == [FILENAME]


def test_download_digest_mismatch_still_raises(
    tmp_path: Path, pin: PinnedFile
) -> None:
    """The archive is a shortcut PAST the network, never past the check."""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    scratch = tmp_path / "scratch"

    download = _fake_download(b"a compromised CDN's bytes")
    with mock.patch("installation.provisioner._download", download):
        with pytest.raises(RuntimeError, match="sha256 mismatch"):
            _fetch_verified(pin, scratch, archive_dir=archive_dir)

    # The bad bytes were neither kept in scratch nor archived.
    assert not (scratch / FILENAME).exists()
    assert list(archive_dir.iterdir()) == []


def test_prune_keeps_pinned_digests_and_drops_the_rest(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    stale_digest = hashlib.sha256(b"an old pin's bytes").hexdigest()
    kept = archive_dir / f"{DIGEST}-{FILENAME}"
    kept.write_bytes(PAYLOAD)
    stale = archive_dir / f"{stale_digest}-old-tool-0.9.0.tar.gz"
    stale.write_bytes(b"an old pin's bytes")

    pins = {
        "tool": {
            "version": "1.0.0",
            "files": {
                "darwin-arm64": {"url": URL, "sha256": DIGEST},
                # camoufox-style aliasing: another target, same digest —
                # membership is by digest across ALL targets.
                "win32-arm64": {"url": URL, "sha256": DIGEST},
            },
        },
    }
    prune_archive_cache(archive_dir, pins)

    assert kept.is_file()
    assert not stale.exists()


def test_prune_ignores_missing_target_specs(tmp_path: Path) -> None:
    """A declared gap ({'missing': reason}) carries no digest to keep."""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    entry = archive_dir / f"{DIGEST}-{FILENAME}"
    entry.write_bytes(PAYLOAD)

    pins = {
        "tool": {
            "version": "1.0.0",
            "files": {
                "darwin-arm64": {"url": URL, "sha256": DIGEST},
                "linux-arm64": {"missing": "upstream ships no such build"},
            },
        },
    }
    prune_archive_cache(archive_dir, pins)

    assert entry.is_file()


def test_prune_tolerates_absent_dir(tmp_path: Path) -> None:
    prune_archive_cache(tmp_path / "never-created", {"tool": {"files": {}}})


def test_prune_drops_orphaned_tmp_files(tmp_path: Path) -> None:
    """A crashed writer's ``.tmp-<uuid>`` never matches a pinned digest."""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    kept = archive_dir / f"{DIGEST}-{FILENAME}"
    kept.write_bytes(PAYLOAD)
    orphan = archive_dir / ".tmp-deadbeefcafe"
    orphan.write_bytes(b"half of an interrupted write-through")

    pins = {
        "tool": {
            "version": "1.0.0",
            "files": {"darwin-arm64": {"url": URL, "sha256": DIGEST}},
        },
    }
    prune_archive_cache(archive_dir, pins)

    assert kept.is_file()
    assert not orphan.exists()


def test_main_accepts_archive_cache_and_prunes_once(tmp_path: Path) -> None:
    """--archive-cache parses, prunes against the pin table, and is threaded.

    Real provisioning is far too heavy for a unit test, so the seams main()
    talks through are mocked and the WIRING is what gets asserted.
    """
    archive_dir = tmp_path / "archives"
    pins = {"tool": {"version": "1.0.0", "files": {}}}
    ok = mock.Mock(ok=True)
    with (
        mock.patch("installation.provisioner.load_pins", return_value=pins),
        mock.patch("installation.provisioner.prune_archive_cache") as prune,
        mock.patch(
            "installation.provisioner.provision_runtimes", return_value=[ok]
        ) as provision,
    ):
        rc = main(["--archive-cache", str(archive_dir)])

    assert rc == 0
    prune.assert_called_once_with(archive_dir, pins)
    assert provision.call_args.kwargs["archive_dir"] == archive_dir


def test_main_without_flag_neither_prunes_nor_archives(tmp_path: Path) -> None:
    ok = mock.Mock(ok=True)
    with (
        mock.patch("installation.provisioner.prune_archive_cache") as prune,
        mock.patch(
            "installation.provisioner.provision_runtimes", return_value=[ok]
        ) as provision,
    ):
        rc = main([])

    assert rc == 0
    prune.assert_not_called()
    assert provision.call_args.kwargs["archive_dir"] is None
