#!/usr/bin/env python3
"""Stage a static APT repository layout (dists/ + pool/) from a pool of .debs.

Pure stdlib. Builds dists/<suite>/{Packages,Packages.gz,Release,InRelease,Release.gpg}
and copies .debs into pool/<first-char>/.

Usage:
  python stage_apt_repo.py --pool POOL_DIR --out OUT_DIR \
      --suite hermes-stable|hermes-nightly [--gpg-key-file PATH]

Exit codes:
  0 - success
  2 - usage/IO error
  3 - Release emitted but not signed (gpg binary or key file missing);
      CI treats 3 as failure
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ARCH = "arm64"
COMPONENT = "main"

REQUIRED_CONTROL_FIELDS = ["Package", "Version", "Architecture"]


class StageError(Exception):
    """Fatal staging error."""


def die(msg: str, code: int = 2) -> "NoReturn":  # type: ignore[valid-type]
    print(f"stage_apt_repo: {msg}", file=sys.stderr)
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# .deb control parsing (stdlib ar + tar, no dpkg-deb)
# ---------------------------------------------------------------------------

def read_ar_entries(data: bytes):
    """Yield (name, size, payload) for each member of an ar archive."""
    if data[:8] != b"!<arch>\n":
        raise StageError("not an ar archive")
    pos = 8
    while pos + 60 <= len(data):
        header = data[pos : pos + 60]
        name = header[0:16].decode("ascii", "replace").strip()
        size_field = header[48:58].decode("ascii", "replace").strip()
        try:
            size = int(size_field)
        except ValueError:
            raise StageError(f"bad ar member size {size_field!r}")
        pos += 60
        yield name, size, data[pos : pos + size]
        pos += size + (size % 2)  # members are 2-byte aligned


def deb_control_fields_and_bytes(deb_path: Path) -> tuple[dict, bytes]:
    """Parse a .deb's control member and return (fields, raw archive bytes).

    The caller gets the raw bytes too so hashing/pool-copy need no re-read.
    """
    data = deb_path.read_bytes()
    control_tar = None
    for name, size, payload in read_ar_entries(data):
        if name in ("control.tar", "control.tar.gz", "control.tar.xz", "control.tar.zst"):
            control_tar = (name, payload)
            break
    if control_tar is None:
        raise StageError(f"{deb_path.name}: no control.tar member found")
    name, payload = control_tar

    if name == "control.tar.gz":
        raw = gzip.decompress(payload)
    elif name == "control.tar":
        raw = payload
    else:
        raise StageError(f"{deb_path.name}: unsupported control compression {name} (need tar or tar.gz)")

    fields: dict = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        for member in tf.getmembers():
            if member.name.lstrip("./") == "control":
                f = tf.extractfile(member)
                if f is None:
                    continue
                fields = _parse_debian_control(f.read().decode("utf-8", "replace"))
                break
    for req in REQUIRED_CONTROL_FIELDS:
        if req not in fields:
            raise StageError(f"{deb_path.name}: control missing {req}")
    return fields, data


def deb_control_fields(deb_path: Path) -> dict:
    """Parse Package/Version/Architecture/... from a .deb's control member."""
    fields, _ = deb_control_fields_and_bytes(deb_path)
    return fields


def _parse_debian_control(text: str) -> dict:
    fields: dict = {}
    last = None
    for line in text.splitlines():
        if not line.strip():
            last = None
            continue
        if line[0] in " \t" and last:
            fields[last] += " " + line.strip()
        elif ":" in line:
            key, _, val = line.partition(":")
            last = key.strip()
            fields[last] = val.strip()
    return fields


# ---------------------------------------------------------------------------
# dpkg version ordering: '~' sorts before end-of-version (and before empty)
# ---------------------------------------------------------------------------

def _order_char(ch: str) -> int:
    if ch == "~":
        return -1
    if ch.isdigit():
        return 0
    if ch.isalpha():
        return ord(ch)
    return ord(ch) + 256


def deb_version_key(version: str):
    """Sort key implementing dpkg version comparison for our versions."""
    epoch, _, rest = version.partition(":")
    epoch_num = int(epoch) if epoch.isdigit() else 0
    if ":" not in version:
        rest = version
    up, _, rev = rest.rpartition("-")
    if not up:
        up, rev = rest, ""

    def cmp_part(s: str):
        parts = []
        i = 0
        while i < len(s):
            if s[i].isdigit():
                j = i
                while j < len(s) and s[j].isdigit():
                    j += 1
                parts.append((0, int(s[i:j]), ""))
                i = j
            else:
                j = i
                while j < len(s) and not s[j].isdigit():
                    j += 1
                parts.append((1, tuple(_order_char(c) for c in s[i:j]), ""))
                i = j
        return parts

    # dpkg: end-of-part sorts after everything except '~'; padding the shorter
    # part with (2, ...) achieves that ('~' yields order char -1 < any pad).
    def padded(parts):
        return parts + [(2, (), "")] * 4

    return (epoch_num, padded(cmp_part(up)), padded(cmp_part(rev)))


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def existing_published(out_dir: Path, suite: str) -> set:
    """(package, version) pairs already published in dists/<suite>/Packages."""
    packages_file = out_dir / "dists" / suite / COMPONENT / f"binary-{ARCH}" / "Packages"
    published = set()
    if packages_file.exists():
        text = packages_file.read_text(encoding="utf-8")
        pkg = ver = None
        for line in text.splitlines():
            if line.startswith("Package: "):
                pkg = line[len("Package: "):].strip()
            elif line.startswith("Version: "):
                ver = line[len("Version: "):].strip()
            elif not line.strip() and pkg and ver:
                published.add((pkg, ver))
                pkg = ver = None
        if pkg and ver:
            published.add((pkg, ver))
    return published


def stage(pool_dir: Path, out_dir: Path, suite: str, gpg_key_file: Path | None) -> int:
    debs = sorted(pool_dir.glob("*.deb"))
    if not debs:
        die(f"no .deb files found in pool {pool_dir}")

    published = existing_published(out_dir, suite)

    dists = out_dir / "dists" / suite
    binary_dir = dists / COMPONENT / f"binary-{ARCH}"
    binary_dir.mkdir(parents=True, exist_ok=True)

    stanzas = []
    for deb in debs:
        fields, raw = deb_control_fields_and_bytes(deb)
        key = (fields["Package"], fields["Version"])
        if key in published:
            die(
                f"refusing: {key[0]}_{key[1]} already published in dists/{suite} "
                "(published apt assets are immutable)"
            )
        arch = fields["Architecture"]
        target = out_dir / "pool" / deb.name[0].lower() / deb.name
        target.parent.mkdir(parents=True, exist_ok=True)
        # Unconditional write: the pool file must always equal the source so
        # the stanza hashes below can never describe a stale/different file.
        target.write_bytes(raw)
        filename = f"pool/{deb.name[0].lower()}/{deb.name}"
        size = len(raw)
        sha256 = hashlib.sha256(raw).hexdigest()
        stanzas.append(
            {
                "Package": fields["Package"],
                "Version": fields["Version"],
                "Architecture": arch,
                "Maintainer": fields.get("Maintainer", "Hermes Agent <noreply@nousresearch.com>"),
                "Installed-Size": fields.get("Installed-Size", "0"),
                "Description": fields.get("Description", "Hermes Agent"),
                "Filename": filename,
                "Size": str(size),
                "SHA256": sha256,
            }
        )

    # Packages sorted by version, nightly (~) below stable
    stanzas.sort(
        key=lambda s: (s["Package"], deb_version_key(s["Version"]))
    )
    packages_text = "\n".join(
        "\n".join(f"{k}: {v}" for k, v in stanza.items()) for stanza in stanzas
    ) + ("\n" if stanzas else "")

    (binary_dir / "Packages").write_text(packages_text, encoding="utf-8")
    with gzip.GzipFile(filename="", mode="wb", fileobj=open(binary_dir / "Packages.gz", "wb"), mtime=0) as gz:
        gz.write(packages_text.encode("utf-8"))

    release_fields = [
        "Origin: Hermes Agent",
        "Label: hermes-agent",
        f"Suite: {suite}",
        f"Codename: {suite}",
        f"Architectures: {ARCH}",
        f"Components: {COMPONENT}",
        f"Description: Hermes Agent apt repository ({suite})",
    ]
    checksums = []
    sha512 = []
    for name in ("Packages", "Packages.gz"):
        p = binary_dir / name
        rel = f"{COMPONENT}/binary-{ARCH}/{name}"
        size = p.stat().st_size
        data = p.read_bytes()
        checksums.append(f" {hashlib.sha256(data).hexdigest()} {size:8d} {rel}")
        sha512.append(f" {hashlib.sha512(data).hexdigest()} {size:8d} {rel}")
    release = "\n".join(release_fields) + "\n"
    release += "\nSHA256:\n" + "\n".join(checksums) + "\n"
    release += "\nSHA512:\n" + "\n".join(sha512) + "\n"

    release_path = dists / "Release"
    release_path.write_text(release, encoding="utf-8")

    if gpg_key_file is not None and shutil.which("gpg"):
        sign(dists, release_path, gpg_key_file)
        return 0
    print("warning: gpg binary or key file unavailable; emitted unsigned Release", file=sys.stderr)
    return 3


def sign(dists: Path, release_path: Path, gpg_key_file: Path) -> None:
    def gpg(*args: str, stdin: bytes | None = None) -> bytes:
        result = subprocess.run(
            ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", *args],
            input=stdin, capture_output=True,
        )
        if result.returncode != 0:
            raise StageError(f"gpg failed: {result.stderr.decode(errors='replace')}")
        return result.stdout

    secret = gpg_key_file.read_bytes()
    gpg("--import", stdin=secret)
    # key file may be a full keypair; extract the key id via listing
    listing = gpg("--list-secret-keys", "--with-colons").decode()
    key_id = None
    for line in listing.splitlines():
        if line.startswith("sec:"):
            key_id = line.split(":")[4]
            break
    if not key_id:
        raise StageError("no secret key found after import")

    gpg(
        "--clearsign", "--local-user", key_id,
        "--output", str(dists / "InRelease"),
        str(release_path),
    )
    gpg(
        "--detach-sign", "--armor", "--local-user", key_id,
        "--output", str(dists / "Release.gpg"),
        str(release_path),
    )


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage a static APT repo layout.")
    ap.add_argument("--pool", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--suite", required=True, choices=["hermes-stable", "hermes-nightly"])
    ap.add_argument("--gpg-key-file", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.pool.is_dir():
        die(f"pool dir not found: {args.pool}")
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        return stage(args.pool, args.out, args.suite, args.gpg_key_file)
    except StageError as e:
        die(str(e))


if __name__ == "__main__":
    sys.exit(main())
