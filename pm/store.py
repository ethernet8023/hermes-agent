"""Machine-wide byte store: download, verify, extract, publish atomically."""

from __future__ import annotations

import os
import platform
import shutil
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

_UA = {"User-Agent": "hermes-pm"}
_LOOPBACK = ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")


ALL_TARGETS = (
    "win32-x64",
    "win32-arm64",
    "linux-x64",
    "linux-arm64",
    "darwin-x64",
    "darwin-arm64",
)


def current_target() -> str:
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64", "x64"):
        arch = "x64"
    else:
        raise RuntimeError(f"unsupported architecture: {platform.machine()}")
    if sys.platform.startswith("win"):
        return f"win32-{arch}"
    if sys.platform == "darwin":
        return f"darwin-{arch}"
    return f"linux-{arch}"


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_url(url: str) -> str:
    """sha256 of a url's content, streamed. `pm lock` uses this to pin."""
    import hashlib
    import urllib.request

    digest = hashlib.sha256()
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=_UA), timeout=600
    ) as resp:
        for block in iter(lambda: resp.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, dest: Path, sha256: str) -> Path:
    import hashlib
    import urllib.request

    """Fetch url into dest dir, hashing while streaming; the digest is proven
    before the caller ever sees the file."""
    if not (url.startswith("https://") or url.startswith(_LOOPBACK)):
        raise ValueError(f"refusing non-https url: {url}")
    dest.mkdir(parents=True, exist_ok=True)
    archive = dest / url.rsplit("/", 1)[-1]

    digest = hashlib.sha256()
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=_UA), timeout=600
    ) as resp, open(archive, "wb") as out:
        for block in iter(lambda: resp.read(1024 * 1024), b""):
            digest.update(block)
            out.write(block)

    actual = digest.hexdigest()
    if actual != sha256:
        archive.unlink(missing_ok=True)
        raise RuntimeError(f"sha256 mismatch for {archive.name}: pinned {sha256}, got {actual}")
    return archive


def extract(archive: Path, dest: Path) -> None:
    import tarfile
    import zipfile

    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith((".tar.gz", ".tgz", ".tar.xz", ".txz")):
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")
    elif name.endswith(".zip"):
        _extract_zip(archive, dest)
    else:
        raise ValueError(f"unsupported archive: {archive.name}")


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        symlinks: list[tuple[zipfile.ZipInfo, str]] = []
        for info in zf.infolist():
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                symlinks.append((info, zf.read(info).decode("utf-8")))
                continue
            written = Path(zf.extract(info, dest))
            if mode & 0o111 and written.is_file():
                written.chmod(mode & 0o777)
        for info, target in symlinks:
            _zip_symlink(info.filename, target, dest)


def _zip_symlink(member: str, target: str, dest: Path) -> None:
    root = dest.resolve()
    link = (root / member).resolve()
    if not link.is_relative_to(root):
        return
    if Path(target).is_absolute():
        return
    resolved = (link.parent / target).resolve()
    if not resolved.is_relative_to(root):
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except OSError:
        link.write_text(target, encoding="utf-8")


def flatten_single_dir(dest: Path) -> None:
    """Hoist a lone top-level dir's contents unless it IS the layout
    (bin/, cmd/, lib/...). Refuses on name collisions."""
    keep = {"bin", "cmd", "lib", "libexec", "share", "etc", "usr"}
    entries = list(dest.iterdir())
    if len(entries) != 1 or not entries[0].is_dir() or entries[0].name in keep:
        return
    inner = entries[0]
    for item in list(inner.iterdir()):
        target = dest / item.name
        if target.exists():
            return
        item.rename(target)
    inner.rmdir()


class Store:
    """One directory of immutable published entries plus a scratch area.
    Downloads are entries too, keyed by hash, so rebuilds never re-fetch."""

    def __init__(self, root: Path):
        self.root = root

    def entry(self, name: str) -> Path:
        return self.root / name

    def published(self, name: str) -> bool:
        return self.entry(name).is_dir()

    def fetch(self, url: str, sha256: str, scratch: Path) -> Path:
        """Verified archive for url, from the store if already fetched.
        The entry is `fetch-<sha256[:16]>/` holding the single file."""
        entry_name = f"fetch-{sha256[:16]}"
        entry = self.entry(entry_name)
        if entry.is_dir():
            files = [p for p in entry.iterdir() if p.is_file()]
            if len(files) == 1 and sha256_file(files[0]) == sha256:
                return files[0]
            shutil.rmtree(entry, ignore_errors=True)
        archive = download(url, scratch, sha256)
        staged = scratch / entry_name
        staged.mkdir(parents=True)
        archive.rename(staged / archive.name)
        published = self.publish(staged, entry_name)
        return published / archive.name

    @contextmanager
    def scratch(self):
        self.root.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def publish(self, staged: Path, name: str) -> Path:
        """Atomic rename into place, retried for Windows file-lock holds
        (Defender, indexers). A concurrent winner's entry is kept."""
        target = self.entry(name)
        delay = 0.5
        for _ in range(5):
            try:
                os.replace(staged, target)
                return target
            except OSError:
                if target.is_dir():
                    return target
                time.sleep(delay)
                delay *= 2
        os.replace(staged, target)
        return target

    @contextmanager
    def install_lock(self):
        """Single-flight advisory lock for all installs on this machine.
        Blocks until acquired: installs legitimately hold it for minutes
        (browser downloads), so the Windows rung polls LK_NBLCK instead of
        msvcrt's 10-second LK_LOCK ladder."""
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".install.lock"
        handle = open(lock_path, "a+b")
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                handle.seek(0)
                waited = 0
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if waited == 5:
                            print("waiting for another hermes install to finish...")
                        time.sleep(1)
                        waited += 1
            else:
                import fcntl

                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    print("waiting for another hermes install to finish...")
                    fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            try:
                if sys.platform.startswith("win"):
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()
