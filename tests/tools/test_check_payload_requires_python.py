"""scripts/check_payload_requires_python.py — the pure decision logic.

The script's job is to notice a pin whose Requires-Python excludes the
payload interpreter. Reaching the index is what the CI job does; these
tests pin the reasoning around it, which is where the subtle faults live:
a marker that already excludes the interpreter is not a failure, and a
version prefix must not match a longer version.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_payload_requires_python",
    REPO_ROOT / "scripts" / "check_payload_requires_python.py",
)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


class TestAccepts:
    """Does a Requires-Python specifier admit the payload interpreter?"""

    @pytest.mark.parametrize(
        "spec,expected",
        [
            (None, True),                    # no constraint: everyone
            ("", True),
            (">=3.8", True),
            (">=3.8.6,<4.0.0", True),
            (">=3.11,<3.14", True),
            (">=3.8.6,<3.11", False),        # backports-strenum 1.3.1
            (">=3.12", False),               # scipy 1.18.0
            (">=3.13", False),               # audioop-lts
        ],
    )
    def test_specifier_verdicts(self, spec, expected):
        assert check.accepts(spec, "3.11.15") is expected

    def test_an_unreadable_specifier_never_fails_a_release(self):
        # A malformed constraint is the package's fault, and pip may well
        # install it anyway. Blocking a release lane on our inability to
        # parse it would be a worse failure than the one being prevented.
        assert check.accepts("this is not a specifier", "3.11.15") is True

    def test_the_boundary_is_the_full_version_not_the_minor(self):
        # `<3.11` excludes every 3.11.x, and `>=3.11.15` includes exactly
        # the pinned patch upward. Comparing on "3.11" alone gets the
        # second case wrong.
        assert check.accepts(">=3.11.15", "3.11.15") is True
        assert check.accepts(">=3.11.16", "3.11.15") is False


class TestMarkers:
    """A pin the target never installs cannot break that target."""

    def test_a_python_version_marker_excludes_the_pin_from_the_audit(self):
        # scipy 1.18.0 requires >=3.12 AND is marked >=3.12, so it is
        # never installed on the 3.11 payload. Reporting it would be a
        # false positive on every target — this is the check that keeps
        # three of the four index hits out of the failure list.
        env = check.marker_env("linux-x64", "3.11.15")
        assert check.marker_admits("python_full_version >= '3.12'", env) is False
        assert check.marker_admits("python_full_version < '3.12'", env) is True

    def test_a_platform_marker_selects_the_targets_that_carry_the_pin(self):
        # backports-strenum is marked sys_platform == 'darwin', which is
        # why only a macOS lane failed.
        marker = "sys_platform == 'darwin'"
        carried = [
            t for t in ("linux-x64", "darwin-arm64", "win32-x64")
            if check.marker_admits(marker, check.marker_env(t, "3.11.15"))
        ]
        assert carried == ["darwin-arm64"]

    def test_an_unreadable_marker_keeps_the_pin_in_the_audit(self):
        # Failing open here would silently drop a pin from the sweep. A
        # pin the checker cannot reason about must stay in scope.
        env = check.marker_env("linux-x64", "3.11.15")
        assert check.marker_admits("sys_platform ===== nonsense", env) is True

    def test_every_target_builds_a_usable_environment(self):
        import tools.lazy_deps as ld

        for target in ld.ALL_TARGETS:
            env = check.marker_env(target, "3.11.15")
            assert env["python_full_version"] == "3.11.15"
            assert env["python_version"] == "3.11"
            # Trivially true markers must evaluate, which proves the
            # environment carries every key PEP 508 needs.
            assert check.marker_admits("python_version >= '3.11'", env) is True


class TestPayloadPython:
    def test_the_audited_version_comes_from_the_pin_table(self):
        # The checker must audit the interpreter the payload actually
        # ships. Reading it from the same file the staging script reads
        # is what stops the two from drifting.
        import json

        pins = json.loads(
            (REPO_ROOT / "installation" / "runtime-pins.json").read_text(encoding="utf-8")
        )
        assert check.payload_python_version() == pins["tools"]["uv"]["python"]
