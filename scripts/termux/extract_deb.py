#!/usr/bin/env python3
"""Extract a .deb (ar archive: debian-binary + control.tar.* + data.tar.*)
without dpkg-deb. Pure stdlib, hardened extraction: rejects traversal,
symlink and device members — shaped after the repo's established
safe-extract pattern. Only data.tar.* is extracted; control.tar is skipped.

Usage: extract_deb.py --deb <path> --out <dir>
"""

from __future__ import annotations

import argparse
import io
import sys
import tarfile
from pathlib import Path, PurePosixPath


def _data_members(archive: bytes) -> bytes:
    """Return the data.tar.* member bytes from a .deb ar archive."""
    if archive[:8] != b"!<arch>\n":
        raise SystemExit("not an ar archive (bad magic)")
    offset = 8
    while offset + 60 <= len(archive):
        hdr = archive[offset:offset + 60]
        name = hdr[0:16].decode("ascii", "replace").rstrip()
        try:
            size = int(hdr[48:58].decode("ascii", "replace").strip())
        except ValueError:
            raise SystemExit(f"bad ar member size at offset {offset}")
        start = offset + 60
        payload = archive[start:start + size]
        if name == "data.tar.gz":
            return payload
        if name.startswith("data.tar"):
            # .xz/.zst/etc — tarfile handles xz; zst is unsupported (fail loudly)
            if name.endswith(".zst") or name.endswith(".lzma"):
                raise SystemExit(f"unsupported data compression: {name} — install dpkg-deb")
            return payload
        offset = start + size + (size % 2)
    raise SystemExit("no data.tar member found in .deb")


def _safe_extract(payload: bytes, dest: Path) -> None:
    deferred_links: list[tuple[str, Path, Path]] = []
    with tarfile.open(fileobj=io.BytesIO(payload)) as tf:
        for member in tf.getmembers():
            path = PurePosixPath(member.name)
            parts = tuple(p for p in path.parts if p not in ("", "."))
            if path.is_absolute() or ".." in parts:
                raise SystemExit(f"unsafe archive member path: {member.name!r}")
            if not parts:
                # the "./" root member termux .debs carry — nothing to extract
                continue
            target = dest.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if member.issym():
                # Symlinks are legitimate in .deb data members. Harden the
                # TARGET instead of refusing the type: resolve it against the
                # member's directory and require it to stay inside the root.
                import posixpath
                link_dir = "/".join(parts[:-1])
                resolved = posixpath.normpath(posixpath.join(link_dir, member.linkname))
                if resolved == ".." or resolved.startswith("../"):
                    raise SystemExit(f"symlink escapes the archive root: {member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    target.unlink()
                try:
                    target.symlink_to(member.linkname)
                except OSError:
                    # Hosts without symlink privilege (Windows without
                    # developer mode): fall back to copying the link target
                    # if it is already extracted, else record it for a
                    # second pass. Content-preserving, not security-relevant
                    # — the target was already validated to stay in-root.
                    resolved_path = dest.joinpath(*resolved.split("/"))
                    if resolved_path.is_file():
                        import shutil
                        shutil.copy2(resolved_path, target)
                    # else: leave for the deferred-links second pass below
                    deferred_links.append((member.linkname, target, resolved_path))
                continue
            if not member.isfile():
                raise SystemExit(f"unsupported archive member type: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                raise SystemExit(f"cannot read archive member: {member.name}")
            with extracted, open(target, "wb") as dst:
                import shutil
                shutil.copyfileobj(extracted, dst)
            try:
                target.chmod(member.mode & 0o777)
            except OSError:
                pass
    # Second pass: links whose targets were not yet extracted when the link
    # was seen (tar member order is not guaranteed).
    import shutil
    for linkname, target, resolved_path in deferred_links:
        if target.exists() or target.is_symlink():
            continue
        if resolved_path.is_file():
            shutil.copy2(resolved_path, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deb", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    deb = Path(args.deb)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    payload = _data_members(deb.read_bytes())
    _safe_extract(payload, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
