"""Package definitions: what a package IS. No versions, no hashes — those
live in lock.json, written by `pm lock`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pm.store import Store


class InstallError(RuntimeError):
    def __init__(self, package: str, cause: str, remedy: str = ""):
        self.package = package
        self.cause = cause
        self.remedy = remedy or "retry, or run `hermes pm doctor`"
        super().__init__(f"{package}: {cause} — {self.remedy}")


def compose_env(diffs: list[dict], base: Optional[dict] = None) -> dict[str, str]:
    """Dependents win over their dependencies for every key: diffs arrive
    deps-first, later ones take precedence — npm's pinned shim must shadow
    the npm bundled inside node, and a package's exports beat inherited env.
    'PATH' values are lists of dirs, prepended."""
    env = dict(os.environ if base is None else base)
    path_dirs: list[str] = []
    for diff in reversed(diffs):
        for key, value in diff.items():
            if key == "PATH":
                dirs = value if isinstance(value, list) else [value]
                path_dirs.extend(str(d) for d in dirs if str(d) not in path_dirs)
    for diff in diffs:
        for key, value in diff.items():
            if key != "PATH":
                env[key] = str(value)
    if path_dirs:
        key = next((k for k in env if k.upper() == "PATH"), "PATH")
        existing = env.get(key, "")
        prefix = os.pathsep.join(path_dirs)
        env[key] = f"{prefix}{os.pathsep}{existing}" if existing else prefix
    return env


class Package:
    """Subclass and register. Declarative for the common case; override
    fetch_url()/unpack()/stage()/verify()/env()/migrate() for the rest.

    name: unique id.
    deps: packages installed before this one.
    optional: not part of the root closure; installed on demand.
    internal: a package manager pm uses inside install steps (uv, npm) —
        refused by pm.run outside pm's own packages.
    on_path: contributes PATH dirs.
    url: template with {version} and {target} holes. override fetch_url()
        when a platform needs a completely different url.
    gaps: targets this package does NOT exist for, with the reason
        (upstream ships no artifact). Everything else is available.
    """

    name: str = ""
    deps: tuple[str, ...] = ()
    optional: bool = False
    internal: bool = False
    on_path: bool = True
    url: str = ""
    gaps: dict[str, str] = {}

    def missing_reason(self, target: str) -> Optional[str]:
        return self.gaps.get(target)

    def fetch_url(self, version: str, target: str) -> str:
        if not self.url:
            raise InstallError(self.name, "package has no download url")
        return self.url.format(version=version, target=target)

    def store_entry(self, version: str, target: str) -> str:
        return f"{self.name}-{version}-{target}"

    def unpack(self, archive: Path, staged: Path, target: str) -> None:
        """Turn the verified archive into the staged tree. Default: extract.
        Override for self-extractors or install-style unpacks (npm)."""
        from pm.store import extract

        extract(archive, staged)

    def stage(self, store: "Store", staged: Path, version: str, target: str) -> None:
        """Post-unpack fixups inside the scratch dir. Default: nothing."""

    def binary(self, entry: Path, target: str) -> Optional[Path]:
        return None

    def verify(self, entry: Path, target: str) -> bool:
        binary = self.binary(entry, target)
        return binary is None or binary.is_file()

    def env(self, entry: Path, target: str) -> dict:
        diff: dict = {}
        if self.on_path:
            binary = self.binary(entry, target)
            if binary is not None:
                diff["PATH"] = [str(binary.parent)]
        return diff

    def migrate(self, previous_version: str, version: str) -> None:
        """User-state migration on version change."""


class StatePackage(Package):
    """A package that is a STATE of this install (the python venv), not a
    store entry. Verified by comparing a stamp; made true by apply()."""

    on_path = False

    def expected_stamp(self, extras: list[str]) -> str:
        raise NotImplementedError

    def apply(self, extras: list[str]) -> None:
        raise NotImplementedError


def machine_matches_binary(binary: Path, target: str) -> Optional[bool]:
    """Does this executable's architecture match the target? Reads the
    PE/ELF/Mach-O header directly. None = unknown format (scripts, shims),
    which is not a mismatch."""
    import struct

    arch = target.rsplit("-", 1)[-1]
    try:
        with open(binary, "rb") as f:
            head = f.read(64)
            if head[:2] == b"MZ":
                f.seek(int.from_bytes(head[60:64], "little"))
                sig = f.read(6)
                if sig[:4] != b"PE\0\0":
                    return None
                machine = int.from_bytes(sig[4:6], "little")
                return machine == {"x64": 0x8664, "arm64": 0xAA64}.get(arch)
            if head[:4] == b"\x7fELF":
                machine = int.from_bytes(head[18:20], "little")
                return machine == {"x64": 0x3E, "arm64": 0xB7}.get(arch)
            if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
                cpu = struct.unpack("<I", head[4:8])[0]
                return cpu == {"x64": 0x01000007, "arm64": 0x0100000C}.get(arch)
            if head[:4] in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
                return True  # universal binary carries both
    except OSError:
        return None
    return None


class Runner:
    """What ensure() hands back: a composed environment and a run mirror."""

    def __init__(self, name: str, env: dict[str, str]):
        self.name = name
        self.env = env

    def run(self, cmd, **kwargs) -> subprocess.CompletedProcess:
        kwargs.setdefault("env", self.env)
        return subprocess.run(cmd, **kwargs)
