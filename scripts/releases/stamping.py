"""Stamp a build tree with a release version. The checkout is never the target.

The regex shapes are the canonical ones ``scripts/release.py`` already uses to
keep every mirror in lockstep. This writer differs in one respect: it takes the
tree as an argument, so a build stamps a copy and the source tree stays at the
placeholder version.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import tomllib
from pathlib import Path


def _rewrite(path: Path, pattern: str, replacement: str, *, count: int = 0, flags: int = 0) -> None:
    if not path.exists():
        return
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    path.write_bytes(re.sub(pattern, replacement, text, count=count, flags=flags).replace("\n", newline).encode("utf-8"))


def stamp(tree: Path, version: str, release_date: str) -> list[Path]:
    """Rewrite every version mirror under ``tree``. Returns the paths written."""
    written: list[Path] = []

    def touch(path: Path) -> None:
        if path.exists():
            written.append(path)

    init = tree / "hermes_cli" / "__init__.py"
    _rewrite(init, r'__release_date__\s*=\s*"[^"]+"', f'__release_date__ = "{release_date}"')
    touch(init)

    generated = tree / "hermes_cli" / "_version.py"
    generated.write_text(
        '"""Generated release identity. Do not commit."""\n\n'
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )
    written.append(generated)

    pyproject = tree / "pyproject.toml"
    _rewrite(pyproject, r'^version\s*=\s*"[^"]+"', f'version = "{version}"', count=1, flags=re.MULTILINE)
    touch(pyproject)

    nix_package = tree / "nix" / "hermes-agent.nix"
    _rewrite(nix_package, r'^  version \? "[^"]+",', f'  version ? "{version}",',
             count=1, flags=re.MULTILINE)
    touch(nix_package)

    desktop = tree / "apps" / "desktop" / "package.json"
    _rewrite(desktop, r'("version"\s*:\s*)"[^"]+"', rf'\g<1>"{version}"', count=1)
    touch(desktop)

    # The lockfile root version is the repo's own and stays put; only the
    # desktop workspace entry mirrors the release.
    lock = tree / "package-lock.json"
    _rewrite(lock, r'("apps/desktop"\s*:\s*\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"version"\s*:\s*)"[^"]+"',
             rf'\g<1>"{version}"', count=1)
    _rewrite(lock, r'("apps/bootstrap-installer"\s*:\s*\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"version"\s*:\s*)"[^"]+"',
             rf'\g<1>"{version}"', count=1)
    touch(lock)

    uv_lock = tree / "uv.lock"
    _rewrite(uv_lock, r'(name = "hermes-agent"\nversion = )"[^"]+"', rf'\g<1>"{version}"', count=1)
    touch(uv_lock)

    installer = tree / "apps" / "bootstrap-installer"
    json_version = rf'\g<1>"{version}"'
    toml_version = f'version = "{version}"'
    for name, pattern, replacement, flags in (
        ("package.json", r'("version"\s*:\s*)"[^"]+"', json_version, 0),
        ("src-tauri/tauri.conf.json", r'("version"\s*:\s*)"[^"]+"', json_version, 0),
        ("src-tauri/Cargo.toml", r'^version\s*=\s*"[^"]+"', toml_version, re.MULTILINE),
        ("src-tauri/Cargo.lock", r'(name = "bootstrap-installer"\nversion = )"[^"]+"', json_version, 0),
    ):
        path = installer / name
        _rewrite(path, pattern, replacement, count=1, flags=flags)
        touch(path)

    # A payload snapshot omits apps/ (INERT_SNAPSHOT_DIRS); it carries no installer to disagree with.
    if (tree / "apps" / "bootstrap-installer").is_dir():
        validate_bootstrap_version(tree, version)
    return written


def validate_bootstrap_version(tree: Path, version: str) -> None:
    installer = tree / "apps" / "bootstrap-installer"
    package = json.loads((installer / "package.json").read_text(encoding="utf-8-sig"))
    tauri = json.loads((installer / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8-sig"))
    cargo = tomllib.loads((installer / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8-sig"))
    root_lock = json.loads((tree / "package-lock.json").read_text(encoding="utf-8-sig"))
    values = {
        "workspace package": package.get("version"),
        "Tauri config": tauri.get("version"),
        "Cargo package": cargo.get("package", {}).get("version"),
        "workspace lock": root_lock.get("packages", {}).get("apps/bootstrap-installer", {}).get("version"),
    }
    cargo_lock = installer / "src-tauri" / "Cargo.lock"
    if cargo_lock.exists():
        lock = tomllib.loads(cargo_lock.read_text(encoding="utf-8-sig"))
        values["Cargo lock"] = next(
            (item.get("version") for item in lock.get("package", [])
             if item.get("name") in {"bootstrap-installer", "hermes-bootstrap"}), None)
    mismatches = {name: value for name, value in values.items() if value != version}
    if mismatches:
        raise ValueError(f"Bootstrap installer version differs from {version}: {mismatches}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-date")
    parser.add_argument("--release-epoch", type=int)
    args = parser.parse_args(argv)
    if args.release_date and args.release_epoch is not None:
        parser.error("--release-date and --release-epoch are mutually exclusive")
    instant = (dt.datetime.fromtimestamp(args.release_epoch, tz=dt.UTC)
               if args.release_epoch is not None else dt.datetime.now(dt.UTC))
    release_date = args.release_date or f"{instant.year}.{instant.month}.{instant.day}"
    stamp(args.tree.resolve(), args.version, release_date)


if __name__ == "__main__":
    main()
