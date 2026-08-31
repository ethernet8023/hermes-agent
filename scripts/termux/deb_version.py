#!/usr/bin/env python3
"""Derive a Debian package version from a hermes-agent release tag.

Pure function; imported by scripts/termux/build_deb.sh and unit-tested by
tests/test_termux_deb_version.py (Task 4 of .hermes/plans/2026-08-31_termux-deb.md).

Mapping:
    v1.2.3                     -> 1.2.3-1
    v1.2.3-nightly.2026083112  -> 1.2.3~nightly.2026083112-1

The ``~`` ranks the nightly below the corresponding stable in dpkg's version
ordering. The major version is capped at 3 digits (CalVer-style cap): a tag
with a 4+ digit major is rejected as malformed.
"""

from __future__ import annotations

import re
import sys

# The nightly timestamp shape MUST match the canonical _NIGHTLY_TAG_RE in
# hermes_cli/update_channel.py (exactly 8 or 14 digits, 20-prefixed) and
# channelForTag in scripts/r2-release.mjs. Cross-referenced by
# tests/test_termux_deb_version.py::test_nightly_tag_shape_matches_canonical.
_TAG_RE = re.compile(
    r"^v(?P<major>\d{1,3})\.(?P<minor>\d{1,3})\.(?P<patch>\d{1,3})"
    r"(?:-nightly\.(?P<ts>20\d{6}(?:\d{6})?))?$"
)


def deb_version_for_tag(tag: str) -> str:
    """Map a release tag to its Debian version. Raises ValueError on malformed tags."""
    m = _TAG_RE.match(tag)
    if m is None:
        raise ValueError(
            f"malformed release tag {tag!r}: expected v<MAJOR>.<MINOR>.<PATCH> "
            "or v<MAJOR>.<MINOR>.<PATCH>-nightly.<timestamp>"
        )
    major = m.group("major")
    if len(major) > 3:
        raise ValueError(f"malformed release tag {tag!r}: major version exceeds 3 digits")
    base = f"{major}.{m.group('minor')}.{m.group('patch')}"
    ts = m.group("ts")
    if ts is None:
        return f"{base}-1"
    return f"{base}~nightly.{ts}-1"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: deb_version.py <tag>", file=sys.stderr)
        return 2
    try:
        print(deb_version_for_tag(argv[1]))
    except ValueError as exc:
        print(f"deb_version: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
