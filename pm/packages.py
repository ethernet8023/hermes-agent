"""Package definitions for the tools hermes manages. Versions and hashes
live in pm/lock.json (written by `pm lock`), never here."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from pm.package import InstallError, Package, StatePackage
from pm.registry import register
from pm.store import Store, flatten_single_dir

_RUST_TRIPLE = {
    "win32-x64": "x86_64-pc-windows-msvc",
    "win32-arm64": "aarch64-pc-windows-msvc",
    "linux-x64": "x86_64-unknown-linux-gnu",
    "linux-arm64": "aarch64-unknown-linux-gnu",
    "darwin-x64": "x86_64-apple-darwin",
    "darwin-arm64": "aarch64-apple-darwin",
}

_NODE_PLAT = {
    "win32-x64": "win-x64",
    "win32-arm64": "win-arm64",
    "linux-x64": "linux-x64",
    "linux-arm64": "linux-arm64",
    "darwin-x64": "darwin-x64",
    "darwin-arm64": "darwin-arm64",
}


class BinaryPackage(Package):
    """A downloaded archive exposing one binary. Covers most tools."""

    binary_rel: dict[str, str] = {}
    flatten = True
    probe_version = True

    def _rel(self, target: str) -> Optional[str]:
        win = target.startswith("win32")
        return self.binary_rel.get(target) or self.binary_rel.get(
            "win32" if win else "posix"
        )

    def stage(self, store: Store, staged: Path, version: str, target: str) -> None:
        if self.flatten:
            flatten_single_dir(staged)

    def binary(self, entry: Path, target: str) -> Optional[Path]:
        rel = self._rel(target)
        return entry / rel if rel else None

    def verify(self, entry: Path, target: str) -> bool:
        binary = self.binary(entry, target)
        if binary is None or not binary.is_file():
            return False
        if not self.probe_version:
            return True
        try:
            proc = subprocess.run(
                [str(binary), "--version"],
                capture_output=True,
                timeout=60,
                env=self._probe_env(),
            )
            return proc.returncode == 0
        except OSError:
            return False

    def _probe_env(self) -> dict:
        """Deps' env composed in: npm's shim is `#!/usr/bin/env node` and
        must find the node it extends on PATH."""
        if not self.deps:
            return dict(os.environ)
        from pm.ensure import env_for

        return env_for(*self.deps)


@register
class Uv(BinaryPackage):
    name = "uv"
    internal = True
    binary_rel = {"win32": "uv.exe", "posix": "uv"}

    def fetch_url(self, version: str, target: str) -> str:
        triple = _RUST_TRIPLE[target]
        ext = "zip" if target.startswith("win32") else "tar.gz"
        return f"https://github.com/astral-sh/uv/releases/download/{version}/uv-{triple}.{ext}"


@register
class Python(BinaryPackage):
    """The payload interpreter (python-build-standalone install_only).
    Optional: dev installs use their own venv's python; bundles stage this
    and point the relocatable venv's pyvenv.cfg at it (pm adopt)."""

    name = "python"
    optional = True
    probe_version = False
    binary_rel = {"win32": "python.exe", "posix": "bin/python3"}

    def fetch_url(self, version: str, target: str) -> str:
        # lock version is "<python>+<release tag>", e.g. "3.11.13+20250807"
        pyver, _, tag = version.partition("+")
        if not tag:
            raise InstallError(self.name, f"version {version!r} needs the +<release> tag")
        triple = _RUST_TRIPLE[target]
        return (
            "https://github.com/astral-sh/python-build-standalone/releases/download/"
            f"{tag}/cpython-{pyver}+{tag}-{triple}-install_only.tar.gz"
        )


def _uv_lock_digest(path: Path) -> bytes:
    """sha256 of uv.lock, cached on (mtime_ns, size) — check() runs at
    every startup and uv.lock is megabyte-class."""
    import hashlib

    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _uv_lock_digest_cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    digest = hashlib.sha256(path.read_bytes()).digest()
    _uv_lock_digest_cache[path] = (key, digest)
    return digest


_uv_lock_digest_cache: dict[Path, tuple] = {}


def uv_env(base_env: Optional[dict] = None) -> dict[str, str]:
    """Sanitized env for pm's internal uv invocations: user-level UV
    overrides and active-venv leakage must not steer which interpreter or
    install dir uv picks (the interpreter-hijack class, #83914)."""
    env = dict(os.environ if base_env is None else base_env)
    for key in list(env):
        if key.startswith("UV_") or key in (
            "VIRTUAL_ENV",
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONSTARTUP",
            "PYTHONEXECUTABLE",
        ):
            env.pop(key)
    env["UV_NO_CONFIG"] = "1"
    return env


@register
class Venv(StatePackage):
    """The project venv: pyproject.toml + uv.lock + enabled extras.
    Made true by `uv sync --frozen`; uv is its internal dependency."""

    name = "venv"
    deps = ("uv",)

    def project_root(self) -> Path:
        from pm.paths import repo_root

        return repo_root()

    def venv_dir(self) -> Path:
        from hermes_constants import project_venv_dir

        found = project_venv_dir(self.project_root())
        return found if found else self.project_root() / "venv"

    def expected_stamp(self, extras: list[str]) -> str:
        import hashlib
        import sys

        h = hashlib.sha256()
        h.update(_uv_lock_digest(self.project_root() / "uv.lock"))
        h.update(",".join(sorted(extras)).encode())
        h.update(f"{sys.version_info.major}.{sys.version_info.minor}".encode())
        return h.hexdigest()

    def apply(self, extras: list[str]) -> None:
        from pm.ensure import uv as pm_uv

        uv_bin, env = pm_uv(venv=self.venv_dir())
        if uv_bin is None:
            raise InstallError(self.name, "uv is not installed")
        cmd = [uv_bin, "sync", "--frozen"]
        for extra in sorted(extras):
            cmd += ["--extra", extra]
        proc = subprocess.run(
            cmd,
            cwd=str(self.project_root()),
            capture_output=True,
            text=True,
            timeout=1800,
            env=env,
        )
        if proc.returncode != 0:
            raise InstallError(
                self.name, f"uv sync exited {proc.returncode}: {proc.stderr[-400:]}"
            )


@register
class Nodejs(BinaryPackage):
    name = "node"
    internal = True
    binary_rel = {"win32": "node.exe", "posix": "bin/node"}

    def fetch_url(self, version: str, target: str) -> str:
        plat = _NODE_PLAT[target]
        ext = "zip" if target.startswith("win32") else "tar.xz"
        return f"https://nodejs.org/dist/v{version}/node-v{version}-{plat}.{ext}"


@register
class Npm(BinaryPackage):
    name = "npm"
    internal = True
    deps = ("node",)
    binary_rel = {"win32": "npm.cmd", "posix": "bin/npm"}
    flatten = False
    probe_version = False
    url = "https://registry.npmjs.org/npm/-/npm-{version}.tgz"

    def unpack(self, archive: Path, staged: Path, target: str) -> None:
        """npm installs itself using the node it extends: a plain unpack
        resolves the cli from dirname(process.execPath) and finds node's
        bundled npm instead. --offline pins the bytes to the verified
        tarball; --ignore-scripts + a sanitized env keep user npm/node
        config out of the staging."""
        from pm.ensure import _facts, _store
        from pm.registry import get_package

        facts = _facts()
        store = _store()
        node = get_package("node")
        node_fact = facts.get("node")
        if node_fact is None:
            raise InstallError(self.name, "npm extends node, which is not installed")
        node_bin = node.binary(store.entry(node_fact["entry"]), target)
        if node_bin is None or not node_bin.is_file():
            raise InstallError(self.name, "node's entry is missing its binary")
        win = target.startswith("win32")
        bundled_cli = (
            node_bin.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
            if win
            else node_bin.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
        )
        if not bundled_cli.is_file():
            raise InstallError(self.name, "node's entry is missing its bundled npm-cli.js")

        env = {
            key: value
            for key, value in os.environ.items()
            if not key.lower().startswith("npm_config_")
            and key not in ("NODE_OPTIONS", "NODE_PATH", "NODE_ENV")
        }
        env["npm_config_cache"] = str(archive.parent / ".npm-cache")

        staged.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [
                str(node_bin), str(bundled_cli), "install", "--global",
                "--prefix", str(staged), "--offline", "--ignore-scripts",
                "--no-audit", "--no-fund", str(archive),
            ],
            capture_output=True,
            text=True,
            timeout=900,
            env=env,
        )
        if proc.returncode != 0:
            raise InstallError(
                self.name, f"self-install exited {proc.returncode}: {proc.stderr[-400:]}"
            )


@register
class Git(BinaryPackage):
    """Windows only: Git for Windows carries the bash.exe contract. POSIX
    uses the system git — a deliberate gap, not an oversight. The tar.bz2
    release asset extracts with stdlib tarfile: no self-extractor, no GUI."""

    name = "git"
    optional = True
    binary_rel = {"win32": "cmd/git.exe"}
    flatten = False
    gaps = {
        "linux-x64": "POSIX uses system git by choice",
        "linux-arm64": "POSIX uses system git by choice",
        "darwin-x64": "POSIX uses system git by choice",
        "darwin-arm64": "POSIX uses system git by choice",
    }

    def fetch_url(self, version: str, target: str) -> str:
        tag, build = version.split("+")
        arch = "arm64" if target.endswith("arm64") else "64-bit"
        return (
            f"https://github.com/git-for-windows/git/releases/download/"
            f"v{tag}.windows.{build}/Git-{tag}.{build}-{arch}.tar.bz2"
        )

    def unpack(self, archive: Path, staged: Path, target: str) -> None:
        """stdlib tar extract, but skip members the data filter refuses —
        the MSYS tree ships dev/fd → /proc/self/fd style links that mean
        nothing on Windows and must not fail the install."""
        import tarfile

        staged.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tf:
            for member in tf:
                try:
                    tf.extract(member, staged, filter="data")
                except (tarfile.FilterError, OSError):
                    continue

    def env(self, entry: Path, target: str) -> dict:
        return {"PATH": [str(entry / "cmd"), str(entry / "usr" / "bin")]}


@register
class Gh(BinaryPackage):
    name = "gh"
    optional = True
    binary_rel = {"win32": "bin/gh.exe", "posix": "bin/gh"}

    def fetch_url(self, version: str, target: str) -> str:
        osname, arch = target.split("-")
        plat = {"win32": "windows", "linux": "linux", "darwin": "macOS"}[osname]
        arch = {"x64": "amd64", "arm64": "arm64"}[arch]
        ext = "zip" if osname in ("win32", "darwin") else "tar.gz"
        return (
            f"https://github.com/cli/cli/releases/download/v{version}/"
            f"gh_{version}_{plat}_{arch}.{ext}"
        )


@register
class Ripgrep(BinaryPackage):
    name = "ripgrep"
    binary_rel = {"win32": "rg.exe", "posix": "rg"}

    def fetch_url(self, version: str, target: str) -> str:
        triple = _RUST_TRIPLE[target].replace("-unknown-linux-gnu", "-unknown-linux-musl")
        ext = "zip" if target.startswith("win32") else "tar.gz"
        return (
            f"https://github.com/BurntSushi/ripgrep/releases/download/{version}/"
            f"ripgrep-{version}-{triple}.{ext}"
        )


@register
class CuaDriver(BinaryPackage):
    name = "cua-driver"
    optional = True
    binary_rel = {"win32": "cua-driver.exe", "posix": "cua-driver"}

    def fetch_url(self, version: str, target: str) -> str:
        arch = {
            "darwin-x64": "darwin-universal",
            "darwin-arm64": "darwin-universal",
            "linux-x64": "linux-x86_64",
            "linux-arm64": "linux-arm64",
            "win32-x64": "windows-x86_64",
            "win32-arm64": "windows-arm64",
        }[target]
        ext = "zip" if target.startswith("win32") else "tar.gz"
        return (
            f"https://github.com/trycua/cua/releases/download/cua-driver-rs-v{version}/"
            f"cua-driver-rs-{version}-{arch}-binary.{ext}"
        )

    def stage(self, store: Store, staged: Path, version: str, target: str) -> None:
        flatten_single_dir(staged)
        sdk = staged / "sdk"
        if sdk.is_dir():
            shutil.rmtree(sdk, ignore_errors=True)

    def _probe_env(self) -> dict:
        """The probe IS a first run: it must not mint telemetry state."""
        try:
            from tools.computer_use.cua_backend import cua_driver_child_env

            return cua_driver_child_env()
        except Exception:
            return dict(os.environ, CUA_DRIVER_RS_TELEMETRY_ENABLED="0")


@register
class AgentBrowser(BinaryPackage):
    name = "agent-browser"
    optional = True
    deps = ("chromium", "chromium-headless-shell")
    flatten = True
    probe_version = False
    url = "https://registry.npmjs.org/agent-browser/-/agent-browser-{version}.tgz"
    gaps = {
        "win32-arm64": "agent-browser ships no win32-arm64 binary",
    }

    def _rel(self, target: str) -> Optional[str]:
        ext = ".exe" if target.startswith("win32") else ""
        return f"bin/agent-browser-{target}{ext}"

    def stage(self, store: Store, staged: Path, version: str, target: str) -> None:
        flatten_single_dir(staged)
        bin_dir = staged / "bin"
        if not bin_dir.is_dir():
            raise InstallError(self.name, "staged without a bin/ directory")
        keep = Path(self._rel(target)).name
        if not (bin_dir / keep).is_file():
            raise InstallError(self.name, f"{keep} missing from the staged tarball")
        for item in bin_dir.iterdir():
            if item.is_file() and item.name.startswith("agent-browser-") and item.name != keep:
                item.unlink()


class PlaywrightBrowser(Package):
    """Playwright resolves browsers by DIRECTORY NAME under one root:
    `<name with '-'→'_'>-<revision>`, no target suffix. The env points
    PLAYWRIGHT_BROWSERS_PATH at the store root itself. The entry carries
    the INSTALLATION_COMPLETE marker playwright checks.

    The version is `<playwright revision>+<chrome version>` — most targets
    download from the Chrome-for-Testing CDN by chrome version, and the
    targets CfT doesn't build (linux-arm64) come from playwright's own
    mirror by revision. The store entry is named by revision only, which
    is all playwright's resolver reads."""

    optional = True
    on_path = False
    _CDN = "https://cdn.playwright.dev"

    # Chrome-for-Testing platform names; targets absent here fall back to
    # playwright's dbazure mirror with its own platform names.
    _CFT = {
        "linux-x64": "linux64",
        "darwin-x64": "mac-x64",
        "darwin-arm64": "mac-arm64",
        "win32-x64": "win64",
    }
    _MIRROR = {"linux-arm64": "linux-arm64"}
    _FILE = ""  # "chrome" / "chrome-headless-shell"
    _MIRROR_FILE = ""  # "chromium" / "chromium-headless-shell"

    def store_entry(self, version: str, target: str) -> str:
        revision = version.partition("+")[0]
        return f"{self.name.replace('-', '_')}-{revision}"

    def fetch_url(self, version: str, target: str) -> str:
        revision, _, chrome = version.partition("+")
        plat = self._CFT.get(target)
        if plat and chrome:
            return f"{self._CDN}/builds/cft/{chrome}/{plat}/{self._FILE}-{plat}.zip"
        mirror_plat = self._MIRROR[target]
        return (
            f"{self._CDN}/dbazure/download/playwright/builds/chromium/"
            f"{revision}/{self._MIRROR_FILE}-{mirror_plat}.zip"
        )

    def stage(self, store: Store, staged: Path, version: str, target: str) -> None:
        (staged / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")

    def verify(self, entry: Path, target: str) -> bool:
        return (entry / "INSTALLATION_COMPLETE").is_file()

    def env(self, entry: Path, target: str) -> dict:
        return {"PLAYWRIGHT_BROWSERS_PATH": str(entry.parent)}


@register
class Chromium(PlaywrightBrowser):
    name = "chromium"
    gaps = {"win32-arm64": "playwright publishes no win-arm64 chromium build"}
    _FILE = "chrome"
    _MIRROR_FILE = "chromium"


@register
class ChromiumHeadlessShell(PlaywrightBrowser):
    name = "chromium-headless-shell"
    gaps = Chromium.gaps
    _FILE = "chrome-headless-shell"
    _MIRROR_FILE = "chromium-headless-shell"
