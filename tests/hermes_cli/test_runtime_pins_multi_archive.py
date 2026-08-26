"""Multi-archive pins: one tool assembled from several downloads.

An upstream sometimes splits ONE runtime across downloads that have to
land in a single directory — llama.cpp ships its CUDA engine and the
cudart DLLs it links against as separate zips, and Windows resolves a
DLL from the loading executable's own directory, so `extends` plus a
PATH edge cannot substitute for them sharing an entry.

These assert the behavior that makes that safe: every archive is
digest-verified, the merge is additive rather than destructive, and a
collision between two archives fails loudly instead of letting
extraction order pick a winner.
"""

import sys

import pytest

import installation.provisioner as rp
import installation.registry as rr

from .test_runtime_provisioner import (  # noqa: F401 — fixtures
    _make_tar,
    _pins_file,
    _script,
    served,
    target,
)

# The provisioner's layout map appends .exe on Windows targets, so a
# fixture archive has to carry the name THIS host resolves to.
GH = "bin/gh.exe" if sys.platform == "win32" else "bin/gh"

# The shared fixtures stage a POSIX shell script as the tool binary, and
# the provisioner verifies an entry by RUNNING it — which Windows cannot
# do with a .exe that is really a shell script. The two tests that need a
# fully published entry are therefore POSIX-only; the ones that assert
# validation and refusal run everywhere. (The merge behaviour itself is
# additionally proven against the real llama.cpp CUDA pair on win-arm64.)
needs_runnable_fixture = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fixture tool binary is a shell script; the publish probe execs it",
)


class TestAlsoArchives:
    @needs_runnable_fixture
    def test_every_archive_lands_in_one_entry(self, served, tmp_path, target):
        """The whole point: files from both archives, side by side."""
        root, base = served
        main = _make_tar(root, "engine.tar.gz", {GH: _script()})
        extra = _make_tar(root, "cudart.tar.gz", {"cudart64_13.dll": b"dll-bytes"})
        pins = _pins_file(tmp_path / "repo", {"gh": {
            "version": "1.0.0",
            "files": {target: {
                "url": f"{base}/engine.tar.gz",
                "sha256": main,
                "also": [{"url": f"{base}/cudart.tar.gz", "sha256": extra}],
            }},
        }})

        rt = tmp_path / "rt"
        results = rp.provision_runtimes(runtime_dir=rt, install_root=pins)

        assert [r.action for r in results] == ["downloaded"], results[0].detail
        entry = rt / f"gh-1.0.0-{target}"
        # The primary archive's payload SURVIVED the extra's extraction —
        # _extract empties its destination, so a naive second call would
        # have deleted this.
        assert (entry / GH).is_file()
        assert (entry / "cudart64_13.dll").read_bytes() == b"dll-bytes"

    def test_a_bad_extra_digest_fails_the_tool(self, served, tmp_path, target):
        """An extra is the same trust decision as the primary artifact."""
        root, base = served
        main = _make_tar(root, "engine2.tar.gz", {GH: _script()})
        _make_tar(root, "tampered.tar.gz", {"cudart64_13.dll": b"dll-bytes"})
        pins = _pins_file(tmp_path / "repo", {"gh": {
            "version": "1.0.0",
            "files": {target: {
                "url": f"{base}/engine2.tar.gz",
                "sha256": main,
                "also": [{"url": f"{base}/tampered.tar.gz", "sha256": "e" * 64}],
            }},
        }})

        rt = tmp_path / "rt"
        results = rp.provision_runtimes(runtime_dir=rt, install_root=pins)

        assert results[0].action == "failed"
        assert "sha256 mismatch" in (results[0].detail or "")
        assert rr.load_facts(rt) == {}

    def test_colliding_archives_fail_instead_of_racing(self, served, tmp_path, target):
        """Two archives claiming one filename is unresolvable, not a
        last-writer-wins situation."""
        root, base = served
        main = _make_tar(root, "engine3.tar.gz", {GH: _script()})
        clash = _make_tar(root, "clash.tar.gz", {GH: _script()})
        pins = _pins_file(tmp_path / "repo", {"gh": {
            "version": "1.0.0",
            "files": {target: {
                "url": f"{base}/engine3.tar.gz",
                "sha256": main,
                "also": [{"url": f"{base}/clash.tar.gz", "sha256": clash}],
            }},
        }})

        rt = tmp_path / "rt"
        results = rp.provision_runtimes(runtime_dir=rt, install_root=pins)

        assert results[0].action == "failed"
        assert "would overwrite" in (results[0].detail or "")

    @needs_runnable_fixture
    def test_the_marker_accounts_for_every_archive(self, served, tmp_path, target):
        """A published entry must be provably complete: recording only the
        primary digest would let a half-assembled tree pass as published."""
        root, base = served
        main = _make_tar(root, "engine4.tar.gz", {GH: _script()})
        extra = _make_tar(root, "cudart4.tar.gz", {"cudart64_13.dll": b"x"})
        pins = _pins_file(tmp_path / "repo", {"gh": {
            "version": "1.0.0",
            "files": {target: {
                "url": f"{base}/engine4.tar.gz",
                "sha256": main,
                "also": [{"url": f"{base}/cudart4.tar.gz", "sha256": extra}],
            }},
        }})

        rt = tmp_path / "rt"
        rp.provision_runtimes(runtime_dir=rt, install_root=pins)

        import json

        marker = json.loads(
            (rt / f"gh-1.0.0-{target}" / rp.ENTRY_MARKER_NAME).read_text(encoding="utf-8")
        )
        assert marker["sha256"] == main
        assert marker["alsoSha256"] == [extra]


class TestAlsoValidation:
    def test_an_extra_needs_an_https_url(self, tmp_path, target):
        pins = _pins_file(tmp_path / "repo", {"gh": {
            "version": "1.0.0",
            "files": {target: {
                "url": "https://example.com/a.tar.gz",
                "sha256": "a" * 64,
                "also": [{"url": "http://evil.example/b.tar.gz", "sha256": "b" * 64}],
            }},
        }})

        with pytest.raises(ValueError, match="also\\[0\\] needs an https url"):
            rr.load_pins(pins)

    def test_an_extra_needs_a_full_digest(self, tmp_path, target):
        pins = _pins_file(tmp_path / "repo", {"gh": {
            "version": "1.0.0",
            "files": {target: {
                "url": "https://example.com/a.tar.gz",
                "sha256": "a" * 64,
                "also": [{"url": "https://example.com/b.tar.gz", "sha256": "short"}],
            }},
        }})

        with pytest.raises(ValueError, match="also\\[0\\] sha256 must be 64 hex chars"):
            rr.load_pins(pins)

    def test_a_pin_without_extras_resolves_to_an_empty_tuple(self, tmp_path, target):
        pins = _pins_file(tmp_path / "repo", {"gh": {
            "version": "1.0.0",
            "files": {target: {"url": "https://example.com/a.tar.gz", "sha256": "a" * 64}},
        }})

        assert rr.pinned_file("gh", target, install_root=pins).also == ()
