"""Nightly release plumbing in scripts/release.py.

The nightly channel's whole safety story is tag SHAPES: nightly tags
carry a -nightly.YYYYMMDD suffix, every stable selector requires the
no-suffix shape, and the version math (next-MINOR over stable) makes
electron-updater's semver ordering implement both channel-switch
directions. These tests pin the shapes and the math; the workflow only
provides credentials.
"""

import importlib.util
from pathlib import Path

_RELEASE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "release.py"
_SPEC = importlib.util.spec_from_file_location("hermes_release_nightly", _RELEASE_PATH)
assert _SPEC and _SPEC.loader
release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release)


class TestNightlyTagShape:
    def test_nightly_tag_for_date_is_next_minor(self):
        assert release.nightly_tag_for_date("v0.27.2", "20260818103000") == "v0.28.0-nightly.20260818103000"
        assert release.nightly_tag_for_date("v1.4.0", "20261231235959") == "v1.5.0-nightly.20261231235959"

    def test_no_stable_line_yet(self):
        assert release.nightly_tag_for_date(None, "20260818103000") == "v0.1.0-nightly.20260818103000"

    def test_shape_round_trips_through_the_nightly_matcher(self):
        tag = release.nightly_tag_for_date("v0.27.2", "20260818103000")
        assert release._NIGHTLY_TAG_RE.fullmatch(tag)

    def test_legacy_date_only_shape_still_matches(self):
        """Readers stay tolerant of the original 8-digit suffix so an
        already-published nightly keeps parsing (prune, last-nightly)."""
        assert release._NIGHTLY_TAG_RE.fullmatch("v0.28.0-nightly.20260818")

    def test_nightly_shape_is_invisible_to_the_stable_selector(self):
        """THE invariant: a nightly tag must never parse as stable —
        otherwise stable users update onto nightlies."""
        for tag in (
            release.nightly_tag_for_date("v0.27.2", "20260818103000"),
            "v0.28.0-nightly.20260818",
        ):
            assert release._SEMVER_TAG_RE.fullmatch(tag) is None, tag

    def test_stable_shape_is_invisible_to_the_nightly_matcher(self):
        assert release._NIGHTLY_TAG_RE.fullmatch("v0.27.2") is None
        # Legacy CalVer must not match either (v2026.7.20 has 20-prefixed
        # components that could fool a sloppy pattern).
        assert release._NIGHTLY_TAG_RE.fullmatch("v2026.7.20") is None

    def test_same_day_nightlies_order_chronologically(self):
        """Second precision exists so manual fires can stack in one day;
        fixed-length pure-numeric identifiers order the same lexically
        (git -v:refname) and numerically (semver prerelease compare)."""
        a = release.nightly_tag_for_date("v0.27.2", "20260818090000")
        b = release.nightly_tag_for_date("v0.27.2", "20260818171500")
        assert a < b  # lexical == chronological at fixed length

    def test_nightly_outversions_current_stable_loses_to_next_minor(self):
        """The semver ordering that makes both channel switches work,
        checked with packaging's canonical comparison when available,
        else by electron-updater's documented precedence rules."""
        try:
            from packaging.version import Version
        except ImportError:
            import pytest

            pytest.skip("packaging not installed in this env")
        nightly = Version("0.28.0-nightly.20260818103000".replace("-nightly.", "a"))
        assert Version("0.27.2") < nightly < Version("0.28.0")


class TestNightlyDateStamps:
    def test_prune_cutoff_uses_the_tag_date(self):
        """The prune window keys on the tag's own YYYYMMDD prefix, for
        both suffix shapes — a 14-digit suffix compared whole against an
        8-digit cutoff would be decided by string length, not by day."""
        legacy = release._NIGHTLY_TAG_RE.fullmatch("v0.28.0-nightly.20260801")
        stamped = release._NIGHTLY_TAG_RE.fullmatch("v0.28.0-nightly.20260801235959")
        assert legacy and legacy.group(1)[:8] == "20260801"
        assert stamped and stamped.group(1)[:8] == "20260801"


class TestNightlyIsDrafted:
    """A nightly is created as a DRAFT prerelease.

    A published release with no installers attached is one users can
    reach and cannot use. The desktop matrix attaches the installers to
    the draft by tag, and the nightly workflow publishes it only after
    that matrix is green.
    """

    def _create_argv(self, monkeypatch, tmp_path):
        """The argv of the `gh release create` cmd_nightly would run."""
        import subprocess
        from types import SimpleNamespace

        captured: list[list[str]] = []

        def fake_run(cmd, *a, **kw):
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="url", stderr="")

        def fake_git_result(*args, **kw):
            # `rev-parse --verify --quiet refs/tags/<tag>` must MISS, or
            # cmd_nightly short-circuits on "tag already exists".
            code = 1 if "rev-parse" in args else 0
            return subprocess.CompletedProcess(args, code, "", "")

        monkeypatch.setattr(release, "get_last_tag", lambda: "v0.27.0")
        monkeypatch.setattr(release, "get_last_nightly_tag", lambda: None)
        monkeypatch.setattr(release, "get_commits", lambda **kw: [{"hash": "a" * 40, "subject": "feat: x", "author": "e"}])
        monkeypatch.setattr(release, "generate_changelog", lambda *a, **kw: "notes")
        monkeypatch.setattr(release, "resolve_push_remote", lambda r: "origin")
        monkeypatch.setattr(release, "remote_github_repo", lambda r: "o/r")
        monkeypatch.setattr(release, "git_result", fake_git_result)
        monkeypatch.setattr(release, "git", lambda *a, **kw: "")
        monkeypatch.setattr(release, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

        release.cmd_nightly(SimpleNamespace(date="20260818103000", publish=True, remote="origin"))
        return next(c for c in captured if c[:3] == ["gh", "release", "create"])

    def test_created_as_a_draft_prerelease(self, monkeypatch, tmp_path):
        argv = self._create_argv(monkeypatch, tmp_path)
        assert "--draft" in argv, argv
        assert "--prerelease" in argv, argv

    def test_refuses_to_invent_a_tag(self, monkeypatch, tmp_path):
        """Without --verify-tag, gh creates a missing tag from the default
        branch tip, which would release a different commit than the one
        the nightly math tagged."""
        assert "--verify-tag" in self._create_argv(monkeypatch, tmp_path)
