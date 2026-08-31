"""Package definitions for the tools hermes manages. Versions and hashes
live in pm/lock.json (written by `pm lock`), never here."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from pm.package import (
    DebPackage,
    InstallError,
    Package,
    StatePackage,
    _entry_listing,
    _missing_reason,
    _probe_reason,
)
from pm.registry import register
from pm.store import ALL_TARGETS, Store, flatten_single_dir, merge_tree

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
    # argv after the binary for the smoke probe. A package whose binary
    # rejects the GNU double-dash form (ffmpeg's BtbN autobuild) overrides.
    probe_args: list[str] = ["--version"]
    # Run the probe with cwd=binary.parent: dlopen'd backends (llama.cpp's
    # cudart) resolve their shared libraries from the working directory.
    probe_cwd = False

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

    def verify(self, entry: Path, target: str) -> str:
        """Return '' when the entry is usable on target, else why not:
        a missing binary, a wrong-arch binary, or a --version probe that
        fails to exec, times out, or exits nonzero."""
        binary = self.binary(entry, target)
        if binary is None:
            return "no binary_rel for this target"
        reason = self._binary_reason(binary, entry, target)
        if reason:
            return reason
        if not self.probe_version:
            return ""
        try:
            proc = subprocess.run(
                [str(binary), *self.probe_args],
                capture_output=True,
                timeout=60,
                cwd=str(binary.parent) if self.probe_cwd else None,
                env=self._probe_env(),
            )
        except OSError as e:
            return f"could not exec {binary} {' '.join(self.probe_args)}: {e}"
        except subprocess.TimeoutExpired:
            return f"{binary} {' '.join(self.probe_args)} timed out after 60s"
        if proc.returncode != 0:
            return _probe_reason(binary, proc)
        return ""

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


_MACOS_MANAGED_PYTHON_IDENTIFIER = "com.nousresearch.hermes.managed-python"


def _macos_sign_managed_python(python: Path) -> bool:
    """Give a downloaded Python a stable macOS code identity."""
    if platform.system() != "Darwin":
        return False

    codesign = shutil.which("codesign")
    if not codesign:
        return False

    requirement = (
        "=designated => identifier "
        f'"{_MACOS_MANAGED_PYTHON_IDENTIFIER}"'
    )
    try:
        signed = subprocess.run(
            [
                codesign,
                "--force",
                "--deep",
                "--sign",
                "-",
                "--timestamp=none",
                "--identifier",
                _MACOS_MANAGED_PYTHON_IDENTIFIER,
                "--requirements",
                requirement,
                str(python),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if signed.returncode != 0:
            return False
        verified = subprocess.run(
            [codesign, "--verify", "--deep", "--strict", str(python)],
            check=False,
            capture_output=True,
            text=True,
        )
        return verified.returncode == 0
    except Exception:
        return False


@register
class Python(BinaryPackage, DebPackage):
    """The payload interpreter (python-build-standalone install_only).
    Optional: dev installs use their own venv's python; bundles stage this
    and point the relocatable venv's pyvenv.cfg at it (pm adopt)."""

    name = "python"
    optional = True
    probe_version = False
    binary_rel = {"win32": "python.exe", "posix": "bin/python3"}
    deb_package = "python3.11"

    def stage(self, store: Store, staged: Path, version: str, target: str) -> None:
        super().stage(store, staged, version, target)
        binary = self.binary(staged, target)
        if binary is not None:
            _macos_sign_managed_python(binary)

    def fetch_url(self, version: str, target: str) -> str:
        if target == "linux-arm64-bionic":
            pyver = version.partition("+")[0]
            return f"https://tur.kcubeterm.com/pool/tur/python3.11_{pyver}_aarch64.deb"
        # lock version is "<python>+<release tag>", e.g. "3.11.13+20250807"
        pyver, _, tag = version.partition("+")
        if not tag:
            raise InstallError(self.name, f"version {version!r} needs the +<release> tag")
        triple = _RUST_TRIPLE[target]
        return (
            "https://github.com/astral-sh/python-build-standalone/releases/download/"
            f"{tag}/cpython-{pyver}+{tag}-{triple}-install_only.tar.gz"
        )
    def unpack(self, archive: Path, staged: Path, target: str) -> None:
        if target == "linux-arm64-bionic":
            DebPackage.unpack(self, archive, staged, target)
        else:
            BinaryPackage.unpack(self, archive, staged, target)

    def binary(self, entry: Path, target: str) -> Optional[Path]:
        if target == "linux-arm64-bionic":
            return None  # bionic binary cannot exec on the staging host
        return BinaryPackage.binary(self, entry, target)

    def verify(self, entry: Path, target: str) -> str:
        if target == "linux-arm64-bionic":
            expected = entry / self.prefix_rel / "bin/python3.11"
            if not (expected.is_file() or expected.is_symlink()):
                return f"python3.11 missing under {self.prefix_rel}/bin"
            return ""
        return BinaryPackage.verify(self, entry, target)




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
class Nodejs(BinaryPackage, DebPackage):
    """nodejs.org tarballs for glibc/mac/win; the Termux main-repo nodejs
    .deb for bionic (same major line, termux-built)."""

    name = "node"
    internal = True
    binary_rel = {"win32": "node.exe", "posix": "bin/node"}
    deb_package = "nodejs"

    def fetch_url(self, version: str, target: str) -> str:
        if target == "linux-arm64-bionic":
            # termux's deb carries a -1 revision after the upstream version
            return f"https://packages.termux.dev/apt/termux-main/pool/main/n/nodejs/nodejs_{version}-1_aarch64.deb"
        plat = _NODE_PLAT[target]
        ext = "zip" if target.startswith("win32") else "tar.xz"
        return f"https://nodejs.org/dist/v{version}/node-v{version}-{plat}.{ext}"
    def unpack(self, archive: Path, staged: Path, target: str) -> None:
        if target == "linux-arm64-bionic":
            DebPackage.unpack(self, archive, staged, target)
        else:
            BinaryPackage.unpack(self, archive, staged, target)

    def binary(self, entry: Path, target: str) -> Optional[Path]:
        if target == "linux-arm64-bionic":
            return None
        return BinaryPackage.binary(self, entry, target)

    def verify(self, entry: Path, target: str) -> str:
        if target == "linux-arm64-bionic":
            expected = entry / self.prefix_rel / "bin/node"
            if not (expected.is_file() or expected.is_symlink()):
                return f"node missing under {self.prefix_rel}/bin"
            return ""
        return BinaryPackage.verify(self, entry, target)




@register
class TermuxDocker(Package):
    """The termux/termux-docker container image, pinned by registry digest.

    The image is never downloaded or unpacked by pm -- docker pulls it by
    digest reference at build time. The lock row exists so the digest is
    pinned in the single pin authority beside every other third-party
    artifact: consumers read the digest string from the lock's url field
    (termux/termux-docker@sha256:...). verify() is presence-shaped: this
    package stages nothing.
    """

    name = "termux-docker"
    optional = True
    # Pure pin: no bytes are staged, so stage_only()/install skip the store
    # entirely -- the digest's consumers (docker pull) verify it.
    pin_only = True

    def missing_reason(self, target: str) -> Optional[str]:
        return None if target == "linux-arm64-bionic" else "docker image target is linux-arm64-bionic"

    def fetch_url(self, version: str, target: str) -> str:
        return f"docker://termux/termux-docker@{version}"

    def unpack(self, archive: Path, staged: Path, target: str) -> None:
        raise InstallError(self.name, "a docker image digest is a pin, not a downloadable artifact")

    def verify(self, entry: Path, target: str) -> str:
        return ""


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
class Ffmpeg(BinaryPackage):
    """Static ffmpeg. GPLv3 builds; always bundled.
    optional=False: ffmpeg is a required runtime tool. Sealed bundles ship
    it baked into the payload (post_update skips provisioning sealed
    installs — the artifact is atomic); dev installs get it re-ensured by
    step_provision_runtimes when the pin bumps. Windows: BtbN/FFmpeg-Builds
    (dated autobuild tag; ships ffprobe too). Linux + macOS:
    ffmpeg.martin-riedl.de (uniform ZIP, published sha256; single-binary —
    no ffprobe)."""

    name = "ffmpeg"
    optional = False
    # martin-riedl (posix) zips are a single `ffmpeg` file at the zip root;
    # BtbN (win32) zips carry bin/ffmpeg.exe under one top-level dir that
    # flatten hoists.
    binary_rel = {"win32": "bin/ffmpeg.exe", "posix": "ffmpeg"}
    flatten = True
    # BtbN autobuild n9.0.1-11-ge47273f4d9 rejects `--version`
    # ("Unrecognized option '-version'", exit 2880417800); `-version` works
    # and is accepted by every ffmpeg build.
    probe_args = ["-version"]

    def fetch_url(self, version: str, target: str) -> str:
        osname, arch = target.split("-")
        if osname == "win32":
            # BtbN ships branch builds; the URL is the immutable dated tag.
            return (
                "https://github.com/BtbN/FFmpeg-Builds/releases/download/"
                f"autobuild-{self._btbn_tag}/{self._btbn_asset(target)}"
            )
        martin = {
            ("linux", "x64"): "linux/amd64/1787074600_9.0.1",
            ("linux", "arm64"): "linux/arm64/1787072884_9.0.1",
            ("darwin", "x64"): "macos/amd64/1787081194_9.0.1",
            ("darwin", "arm64"): "macos/arm64/1787073674_9.0.1",
        }
        return f"https://ffmpeg.martin-riedl.de/download/{martin[(osname, arch)]}/ffmpeg.zip"

    def _btbn_tag(self) -> str:
        return "2026-08-28-17-08"

    def _btbn_asset(self, target: str) -> str:
        arch = "arm64" if target.endswith("arm64") else "64"
        return (
            "ffmpeg-n9.0.1-11-ge47273f4d9-"
            f"win{arch}-gpl-9.0.zip"
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
    # No win32-arm64 gap: agent-browser ships only win32-x64, and Windows
    # ARM64 runs it via built-in emulation (its own postinstall falls back
    # to x64 on arm64). chromium is likewise the x64 build on win32-arm64.
    emulated_arch_targets = frozenset({"win32-arm64"})

    def _rel(self, target: str) -> Optional[str]:
        ext = ".exe" if target.startswith("win32") else ""
        # Windows ARM64 runs the x64 binary under built-in emulation:
        # agent-browser ships no native arm64 build (its own postinstall
        # falls back to x64 on arm64), so the staged name is win32-x64.
        if target == "win32-arm64":
            target = "win32-x64"
        return f"bin/agent-browser-{target}{ext}"

    def binary(self, entry: Path, target: str) -> Optional[Path]:
        # The win32-arm64 payload carries the x64 binary (emulated), so
        # resolve it under the win32-x64 name.
        return super().binary(entry, "win32-x64" if target == "win32-arm64" else target)

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
    # win32-arm64 uses the win64 (x64) build: CfT publishes no native
    # win-arm64 chromium, and Windows ARM64 runs x64 binaries via built-in
    # emulation — the same choice agent-browser's own postinstall makes.
    _CFT = {
        "linux-x64": "linux64",
        "darwin-x64": "mac-x64",
        "darwin-arm64": "mac-arm64",
        "win32-x64": "win64",
        "win32-arm64": "win64",
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

    def verify(self, entry: Path, target: str) -> str:
        marker = entry / "INSTALLATION_COMPLETE"
        if marker.is_file():
            return ""
        return f"INSTALLATION_COMPLETE missing under {entry}; {_entry_listing(entry)}"

    def env(self, entry: Path, target: str) -> dict:
        return {"PLAYWRIGHT_BROWSERS_PATH": str(entry.parent)}


@register
class Chromium(PlaywrightBrowser):
    name = "chromium"
    _FILE = "chrome"
    _MIRROR_FILE = "chromium"


@register
class ChromiumHeadlessShell(PlaywrightBrowser):
    name = "chromium-headless-shell"
    _FILE = "chrome-headless-shell"
    _MIRROR_FILE = "chromium-headless-shell"


class LlamaCpp(BinaryPackage):
    """One llama.cpp backend build. Backends are dlopen'd plugins, so a
    usable engine is one archive per (target, backend) — plus, for Windows
    CUDA, the cudart archive: end users have no CUDA toolkit, and Windows
    resolves a DLL from the loading executable's own directory, so those
    DLLs must land beside llama-server.exe rather than in a second entry.

    Backend is a HARDWARE choice, not a target, so each backend is its own
    optional package and the runtime asks for the one this machine can
    use. Version is llama.cpp's rolling release tag without the `b`.
    """

    optional = True
    on_path = False
    binary_rel = {"win32": "llama-server.exe", "posix": "llama-server"}
    flatten = False
    # --version is llama-server's liveness proof AND the check that the
    # backend's shared libraries resolve: a CUDA build with no cudart
    # beside it fails here rather than at first chat.
    probe_cwd = True

    backend: str = ""
    # Release-asset infix per target, or absent where upstream ships none.
    assets: dict[str, str] = {}

    @property
    def gaps(self) -> dict[str, str]:  # type: ignore[override]
        return {
            target: f"llama.cpp publishes no {self.backend} build for {target}"
            for target in ALL_TARGETS
            if target not in self.assets
        }

    def _asset_names(self, version: str, target: str) -> list[str]:
        ext = "zip" if target.startswith("win32") else "tar.gz"
        return [f"llama-b{version}-bin-{self.assets[target]}.{ext}"]

    def fetch_urls(self, version: str, target: str) -> list[str]:
        return [
            f"https://github.com/ggml-org/llama.cpp/releases/download/b{version}/{asset}"
            for asset in self._asset_names(version, target)
        ]

    def fetch_url(self, version: str, target: str) -> str:
        return self.fetch_urls(version, target)[0]

    def known_sha256(self, version: str, url: str) -> Optional[str]:
        """GitHub's release API serves every asset's digest, so pinning a
        280 MB engine costs one API call instead of the download."""
        return _github_release_digests("ggml-org/llama.cpp", f"b{version}").get(
            url.rsplit("/", 1)[-1]
        )

    def stage(self, store: Store, staged: Path, version: str, target: str) -> None:
        """Some archives nest the binaries under build/bin; hoist them so
        binary_rel is one path for every target."""
        if (staged / self.binary(staged, target).name).is_file():
            return
        found = sorted(staged.rglob(self.binary(staged, target).name))
        if not found:
            raise InstallError(self.name, "archive contains no llama-server")
        merge_tree(found[0].parent, staged)


def _github_release_digests(repo: str, tag: str) -> dict[str, str]:
    import json
    import urllib.request

    cached = _release_digest_cache.get((repo, tag))
    if cached is not None:
        return cached
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "hermes-pm"}),
            timeout=120,
        ) as resp:
            release = json.load(resp)
    except Exception:
        return {}
    digests = {}
    for asset in release.get("assets", []):
        digest = (asset.get("digest") or "").partition("sha256:")[2]
        if digest:
            digests[asset["name"]] = digest
    _release_digest_cache[(repo, tag)] = digests
    return digests


_release_digest_cache: dict[tuple, dict] = {}


@register
class LlamaCppCuda(LlamaCpp):
    """Windows only: upstream publishes no prebuilt Linux CUDA archive at
    current tags, so NVIDIA Linux users run the vulkan build."""

    name = "llamacpp-cuda"
    backend = "cuda"
    # CUDA 13.3 verified against 13.1/13.2 drivers; arm64 prebuilts landed
    # on 13.4 (the only CUDA line upstream builds for win-arm64).
    assets = {
        "win32-x64": "win-cuda-13.3-x64",
        "win32-arm64": "win-cuda-13.4-arm64",
    }
    _CUDART = {"win32-x64": "13.3-x64", "win32-arm64": "13.4-arm64"}

    def _asset_names(self, version: str, target: str) -> list[str]:
        return super()._asset_names(version, target) + [
            f"cudart-llama-bin-win-cuda-{self._CUDART[target]}.zip"
        ]


@register
class LlamaCppVulkan(LlamaCpp):
    name = "llamacpp-vulkan"
    backend = "vulkan"
    assets = {
        "win32-x64": "win-vulkan-x64",
        "linux-x64": "ubuntu-vulkan-x64",
        "linux-arm64": "ubuntu-vulkan-arm64",
    }


@register
class LlamaCppMetal(LlamaCpp):
    """macOS archives are unified builds with Metal compiled in."""

    name = "llamacpp-metal"
    backend = "metal"
    assets = {"darwin-x64": "macos-x64", "darwin-arm64": "macos-arm64"}


@register
class LlamaCppCpu(LlamaCpp):
    name = "llamacpp-cpu"
    backend = "cpu"
    assets = {
        "win32-x64": "win-cpu-x64",
        "win32-arm64": "win-cpu-arm64",
        "linux-x64": "ubuntu-x64",
        "linux-arm64": "ubuntu-arm64",
        "darwin-x64": "macos-x64",
        "darwin-arm64": "macos-arm64",
    }
