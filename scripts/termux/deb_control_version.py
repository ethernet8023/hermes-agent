#!/usr/bin/env python3
"""Print the Version field of a .deb's control stanza for a package sanity
check. Pure stdlib ar+tar parsing — never executes anything from the .deb.

Usage: deb_control_version.py --deb <path> --package <name>
Exits non-zero if the stanza is missing or the Package field mismatches.
"""

from __future__ import annotations

import argparse
import io
import sys
import tarfile


def _control_stanza(archive: bytes) -> dict[str, str]:
    if archive[:8] != b"!<arch>\n":
        raise SystemExit("not an ar archive (bad magic)")
    offset = 8
    while offset + 60 <= len(archive):
        hdr = archive[offset:offset + 60]
        name = hdr[0:16].decode("ascii", "replace").rstrip()
        try:
            size = int(hdr[48:58].decode("ascii", "replace").strip())
        except ValueError:
            raise SystemExit("bad ar member size")
        start = offset + 60
        payload = archive[start:start + size]
        if name.startswith("control.tar"):
            with tarfile.open(fileobj=io.BytesIO(payload)) as tf:
                for member in tf.getmembers():
                    if member.name in ("./control", "control"):
                        f = tf.extractfile(member)
                        if f is None:
                            raise SystemExit("cannot read control member")
                        stanza: dict[str, str] = {}
                        for line in f.read().decode("utf-8", "replace").splitlines():
                            if ": " in line:
                                k, v = line.split(": ", 1)
                                stanza[k] = v.strip()
                            elif line.endswith(":") and line:
                                stanza[line[:-1]] = ""
                        return stanza
            raise SystemExit("control.tar has no control member")
        offset = start + size + (size % 2)
    raise SystemExit("no control.tar member found in .deb")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deb", required=True)
    parser.add_argument("--package", required=True)
    args = parser.parse_args(argv)
    stanza = _control_stanza(open(args.deb, "rb").read())
    got_pkg = stanza.get("Package", "")
    if got_pkg != args.package:
        print(f"package mismatch: control says {got_pkg!r}, expected {args.package!r}", file=sys.stderr)
        return 1
    version = stanza.get("Version", "")
    if not version:
        print("control stanza has no Version", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
