"""Provision managed runtime tools into the machine-wide tool store.

THE one dep engine: `hermes update` (post-update MACHINE_STEPS), the
installers (`python -m installation.provisioner`, after the pinned-uv
bootstrap), and the desktop payload staging all run this same code.

Per tool: read the EXACT pin for this target (url + sha256) → download →
verify the digest BEFORE extracting → stage into a scratch directory →
PUBLISH it into the store under ``<tool>-<version>-<target>`` with one
atomic rename → verify by RUNNING the binary → record the fact. A tool
that cannot be verified is not recorded: readers see it as unprovisioned
and fall back to system PATH, and the next run retries.

Bytes are shared, facts are not. The store is machine-wide, so several
installs (44 git worktrees, on the machine this was measured on) share
one copy of node rather than holding ~495MB each. Two rules make that
safe, and they are the whole concurrency story:

* **Publish atomically.** Staging happens in a scratch dir and lands with
  ``os.replace``, so a reader never sees a half-extracted entry.
* **Never mutate a published entry.** The name carries the version and
  the target, so an entry that exists already IS the pinned artifact —
  and another install may be executing it right now. A pin bump creates a
  NEW entry and repoints this install's fact at it.

Tools are visited in the pin table's dependency order, so a tool that
declares ``extends`` is staged after what it extends — npm is unpacked by
running the node it extends. The same edge, read the other way, is the
PATH order recorded into the facts file for both language readers.

There is no salvage and no "reuse whatever is lying around". A tool is
either the exact pinned artifact, verified by digest, or it is absent.
Adopting an unverified tree from a previous install would defeat the
point of pinning digests at all.

Progress streams as installer stage-JSON lines when --json is on, so the
GUI install driver renders provisioning natively.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Callable, Optional

from installation.paths import get_install_root, resolve_bases
from installation.tree import Sealed, runtime_tree
from installation.registry import (
    PLAYWRIGHT_BROWSER_TOOLS,
    PinnedFile,
    RuntimeFact,
    UnavailableOnTarget,
    current_target,
    extends_closure,
    install_order,
    is_optional,
    load_facts,
    load_pins,
    path_order,
    pinned_file,
    requires_closure,
    save_facts,
    store_entry_name,
)

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "hermes-agent-provisioner"}


def _is_windows() -> bool:
    return sys.platform.startswith("win")


# ─── download + verify + extract ────────────────────────────────────────────


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=_UA), timeout=600
    ) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fetch_verified(
    pin: PinnedFile, into: Path, archive_dir: Path | None = None
) -> Path:
    """Download a pinned artifact and prove it is the pinned bytes.

    The digest check happens BEFORE anything is unpacked or executed: a
    mismatched archive is deleted, never extracted. This is the only
    thing standing between a compromised CDN and a user's machine.

    *archive_dir* is a TRANSPORT shortcut, never a trust shortcut: a
    build cache of verified downloads keyed ``<sha256>-<filename>``. A
    hit is re-hashed exactly like a download before it is used, and a
    fresh verified download is written through so the next build skips
    the network. Only the desktop payload staging passes it — a normal
    install (and `hermes update`) never grows an archive dir.
    """
    archive = into / pin.filename

    if archive_dir is not None:
        candidate = archive_dir / f"{pin.sha256}-{pin.filename}"
        if candidate.is_file():
            if _sha256(candidate) == pin.sha256:
                archive.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(candidate, archive)
                return archive
            # The name promises bytes the file does not hold (truncated
            # write, bit rot, tampering). A cache entry is junk the
            # moment it fails its own name — delete it and download.
            logger.warning(
                "archive cache entry %s fails its own digest; discarding",
                candidate.name,
            )
            candidate.unlink(missing_ok=True)

    _download(pin.url, archive)

    actual = _sha256(archive)
    if actual != pin.sha256:
        archive.unlink(missing_ok=True)
        raise RuntimeError(
            f"sha256 mismatch for {pin.filename}: "
            f"pinned {pin.sha256}, downloaded {actual}"
        )

    if archive_dir is not None:
        # Write-through, atomically: a concurrent reader of the cache
        # must never see a partial entry, so land it with one rename.
        archive_dir.mkdir(parents=True, exist_ok=True)
        tmp_entry = archive_dir / f".tmp-{uuid.uuid4().hex}"
        shutil.copyfile(archive, tmp_entry)
        os.replace(tmp_entry, archive_dir / f"{pin.sha256}-{pin.filename}")

    return archive


def prune_archive_cache(archive_dir: Path, pins: dict[str, dict]) -> None:
    """Drop archive-cache entries no pin references any more.

    Membership is by DIGEST across the whole table, not by target or
    tool: one digest may legitimately serve several targets when a pin
    aliases them to the same artifact. An entry survives iff the
    ``<sha256>`` prefix of its name appears anywhere in the pin table —
    everything else (old pins, orphaned tmp files) is dead weight the
    cache would otherwise carry forever.
    """
    if not archive_dir.is_dir():
        return
    live = {
        spec["sha256"]
        for entry in pins.values()
        for spec in entry.get("files", {}).values()
        if isinstance(spec, dict) and "sha256" in spec
    }
    for item in archive_dir.iterdir():
        if not item.is_file():
            continue
        digest = item.name.split("-", 1)[0]
        if digest not in live:
            logger.debug("pruning archive cache entry %s", item.name)
            item.unlink(missing_ok=True)


def _extract(archive: Path, dest: Path) -> None:
    """Extract tar.gz/tar.xz/zip into a freshly emptied *dest*."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith((".tar.gz", ".tgz", ".tar.xz")):
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")
    elif name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            symlinks: list[tuple[zipfile.ZipInfo, str]] = []
            for info in zf.infolist():
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    # A symlink entry's file content IS its target path.
                    # zipfile.extract() would write that path out as a
                    # regular file, which shatters any layout built on
                    # links — a macOS .framework loses Versions/Current
                    # and codesign refuses the whole bundle, even for a
                    # fresh re-sign (proven against the chromium CfT
                    # archive on a real mac).
                    symlinks.append((info, zf.read(info).decode("utf-8")))
                    continue
                # extract() RETURNS the path it actually wrote, with the
                # entry name already sanitized (".." stripped, absolute
                # paths made relative). Chmod that, never info.filename:
                # an entry named "../../victim" chmods a file OUTSIDE the
                # destination, which is an arbitrary chmod +x for anyone
                # who can serve us an archive.
                written = Path(zf.extract(info, dest))
                if mode & 0o111 and written.is_file():
                    written.chmod(mode & 0o777)
            # Links are recreated AFTER every regular entry, so no entry
            # is ever written THROUGH a link — the link-then-file shape
            # of a zip-slip archive dies here: the file lands first as a
            # real path, and the later link then fails on the collision.
            for info, target in symlinks:
                _zip_symlink(info.filename, target, dest)
    else:
        raise ValueError(f"unsupported archive: {archive.name}")


def _zip_symlink(member: str, target: str, dest: Path) -> None:
    """Recreate one zip symlink entry under *dest*, or skip it.

    Skipping (not raising) mirrors how the rest of extraction treats
    malformed entries: one bad member must not kill a 600-file tool
    install. Three shapes are refused:

    - a LINK PATH that escapes dest ("../victim") — same rule
      zipfile.extract() applies to regular entries;
    - an absolute TARGET ("/etc/passwd") — meaningless inside a staged
      tree, dangerous outside it;
    - a relative TARGET that walks above dest from the link's own dir
      (lib/x -> ../../../victim). Links that stay inside the tree
      (Current -> A, Resources -> Versions/Current/Resources) pass.

    On hosts where os.symlink itself fails (Windows without the
    SeCreateSymbolicLink privilege), the entry degrades to the OLD
    behavior — the target path as a plain file — rather than failing
    the tool. Nothing that RUNS on such a host needs the links; the
    macOS bundles that do are never staged for it.
    """
    rel = Path(member)
    if rel.is_absolute() or ".." in rel.parts:
        logger.warning("skipping zip symlink with escaping path: %r", member)
        return
    if Path(target).is_absolute():
        logger.warning("skipping zip symlink %r: absolute target %r", member, target)
        return
    link_path = dest / rel
    link_dir = (dest / rel.parent).resolve()
    resolved = Path(os.path.normpath(link_dir / target))
    if not resolved.is_relative_to(dest.resolve()):
        logger.warning("skipping zip symlink %r: target %r escapes the tree", member, target)
        return
    link_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link_path)
    except OSError as exc:
        logger.warning("zip symlink %r unsupported here (%s); writing target as file", member, exc)
        link_path.write_text(target, encoding="utf-8")


# Directory names that are part of a tool's OWN layout. An archive whose
# single top-level entry is one of these is already unwrapped — hoisting
# it would destroy the layout (a lone `bin/` became `gh` and `gh/bin/gh`
# vanished).
_LAYOUT_DIRS = frozenset({"bin", "cmd", "lib", "libexec", "share", "etc", "usr"})


def _flatten_single_dir(dest: Path) -> None:
    """Hoist a lone VERSIONED wrapper dir's contents up one level.

    Most projects nest everything under one dir named for the release
    (``gh_2.97.0_linux_amd64/``, ``node-v26.7.0-linux-x64/``), which would
    otherwise leak the version into every facts path and break on the
    next bump. Some archives unpack flat instead — same tool, different
    platform, in uv's case — so this keys off what is actually there.
    """
    # EVERY entry counts, dotfiles included. Skipping them made a
    # top-level ".config" invisible to this check, so an archive shaped
    # {".config", "wrapper/.config"} looked like a lone wrapper and the
    # move silently replaced the outer file.
    entries = list(dest.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        return

    inner = entries[0]
    if inner.name.lower() in _LAYOUT_DIRS:
        return

    # Never overwrite. After the checks above the destination holds only
    # `inner`, so the sole way to collide is a child named like its own
    # parent ("gh/gh"); shutil.move's own error for that case names a
    # temp path and reads like a bug in us. Refuse the whole flatten
    # instead: the unflattened tree is merely ugly, a clobbered file is
    # data loss.
    collisions = [c.name for c in inner.iterdir() if (dest / c.name).exists()]
    if collisions:
        raise RuntimeError(
            f"cannot unwrap {inner.name}/: would overwrite {', '.join(sorted(collisions))}"
        )

    for child in inner.iterdir():
        shutil.move(str(child), dest / child.name)
    inner.rmdir()


def _probe_version(
    binary: Path,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 30,
) -> Optional[str]:
    """Run `<binary> --version` and return the first version-shaped token.

    None when the binary does not run — callers treat that as
    unprovisioned, never as fatal.

    Two Windows shapes defeat the exec probe alone, and the pinned
    Chromium pair is one of each (measured against the 145.0.7632.6 CfT
    payload on Windows 11):

    * ``chrome.exe`` is a GUI-subsystem binary. ``--version`` is not a
      console command there — it OPENS THE BROWSER and never exits, so
      the probe burns its whole timeout and leaks the process tree. Its
      PE carries a VERSIONINFO resource, so the version comes from the
      file instead of from the process.
    * ``chrome-headless-shell.exe`` is the mirror image: ``--version``
      prints and exits, and its PE VERSIONINFO is EMPTY.

    So the probe runs the exec rung first, then the file rung. A binary
    that never exits is not spawned at all. See ``_version_probe_plan``.
    """
    plan = _version_probe_plan(binary, args, sys.platform == "win32")
    out = ""
    if plan.exec_args is not None:
        try:
            proc = subprocess.run(
                [str(binary)] + plan.exec_args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
            # stderr counts: printing the version banner there is a common
            # convention (llama-server does), and this probe only asks
            # "did it run and say something version-shaped".
            out = f"{proc.stdout}\n{proc.stderr}"
        except (OSError, subprocess.SubprocessError):
            # TimeoutExpired IS a SubprocessError. Returning here would
            # skip the file-resource rung for the exact shape that needs
            # it most, so fall through instead of reporting no version.
            out = ""
    import re as _re

    m = _re.search(r"\d+(?:\.\d+)+", out or "")
    if m:
        return m.group(0)
    # Upstreams that NUMBER builds rather than release semver print a bare
    # integer (llama.cpp's `version: 10362`, the same shape their pin
    # carries). Only consulted when no dotted version appeared, so a
    # normal tool's version still wins.
    m = _re.search(r"\bversion:?\s+(\d+)\b", out or "", _re.IGNORECASE)
    if m:
        return m.group(1)
    if plan.read_file_version:
        return _windows_file_version(binary)
    return None


@dataclass(frozen=True)
class _ProbePlan:
    """How to ask ONE binary for its version.

    ``exec_args`` is None for a binary that must not be spawned with
    ``--version`` because it does not exit.
    """

    exec_args: Optional[list[str]]
    read_file_version: bool


def _version_probe_plan(
    binary: Path, args: list[str] | None, windows: bool
) -> _ProbePlan:
    """Pick the version rungs that fit this binary.

    An explicit ``args`` from the caller always wins: it is a statement
    that these arguments do terminate.
    """
    if args is not None:
        return _ProbePlan(exec_args=args, read_file_version=windows)
    # chrome.exe --version opens the browser instead of printing, on
    # Windows only: the POSIX builds print and exit like any console
    # tool. The skipped spawn also stops a timed-out probe from leaving
    # an orphaned browser behind on the build machine.
    if windows and _is_gui_chrome(binary):
        return _ProbePlan(exec_args=None, read_file_version=True)
    return _ProbePlan(exec_args=["--version"], read_file_version=windows)


def _is_gui_chrome(binary: Path) -> bool:
    """The full browser, as opposed to the headless shell beside it.

    PureWindowsPath splits on both separators, so a Windows path stays
    readable to a POSIX host that reasons about a Windows payload.
    """
    return PureWindowsPath(str(binary)).name.lower() == "chrome.exe"


def _renders_a_page(binary: Path, env: dict[str, str] | None = None) -> bool:
    """Prove a browser RUNS, for one that cannot answer ``--version``.

    A version out of the PE resource is a file read: it says nothing
    about whether the binary executes, which is the question the
    provisioning probe exists to answer. A headless about:blank render
    is the cheapest real answer — it starts the browser, prints the DOM,
    and exits by itself (measured at 0.9s against the pinned CfT
    payload, and it leaves no stray process).
    """
    try:
        proc = subprocess.run(
            [
                str(binary),
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--dump-dom",
                "about:blank",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "<html" in (proc.stdout or "").lower()


def _windows_file_version(binary: Path) -> Optional[str]:
    """The PE VERSIONINFO product version, or None.

    PowerShell reads the resource without executing the binary. The
    path travels in an environment variable, never spliced into the
    script text — a path containing quotes cannot change the command.
    """
    cmd = "(Get-Item -LiteralPath $env:HERMES_PROBE_PATH).VersionInfo.ProductVersion"
    probe_env = dict(os.environ, HERMES_PROBE_PATH=str(binary))
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=probe_env,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    import re as _re

    m = _re.search(r"\d+(?:\.\d+)+", out or "")
    return m.group(0) if m else None


def _probe_env(
    entry: dict, facts_dir: Path, store: Path, tool: str | None = None
) -> Optional[dict[str, str]]:
    """Environment for the run-the-binary check.

    Most tools are self-contained executables and need nothing. Two
    shapes do:

    * A tool that EXTENDS another is a script launched by it — npm's shim
      is ``#!/usr/bin/env node`` — so the probe has to see the runtime
      dir's own tools on PATH, or it reports "does not run" on any host
      without a system copy and the tool is never recorded.
    * cua-driver ships telemetry ON by default upstream, and its very
      first run is what mints an installation id. The probe IS a first
      run, so it carries the same opt-out env every other cua-driver
      child gets — provisioning a driver must not create the telemetry
      state the user never opted into.
    """
    if tool == "cua-driver":
        return _cua_driver_probe_env()
    if not entry.get("extends"):
        return None
    from installation.env import with_managed_runtimes

    return with_managed_runtimes(runtime_dir=facts_dir, store_dir=store)


def _cua_driver_probe_env() -> dict[str, str]:
    """cua-driver's child env, with the Hermes telemetry policy applied.

    ``tools.computer_use.cua_backend`` owns the policy (opt-in via
    ``computer_use.cua_telemetry``, disabled otherwise). The import is
    local and guarded: ``installation`` is the dependency-free install
    engine and runs in contexts where the agent package is not
    importable, so a missing runtime degrades to disabling telemetry
    outright rather than failing the provision — the same
    fail-safe-toward-off direction the policy itself takes.
    """
    try:
        from tools.computer_use.cua_backend import cua_driver_child_env

        return cua_driver_child_env()
    except Exception:
        return dict(os.environ, CUA_DRIVER_RS_TELEMETRY_ENABLED="0")


# ─── per-tool layout + staging ──────────────────────────────────────────────


@dataclass
class ToolResult:
    tool: str
    # kept | adopted | downloaded | system | unavailable | failed
    #
    # "unavailable" is a DECLARED gap (the pin table's {"missing": reason}
    # rows): the capability does not
    # exist on this target, so nothing was attempted and nothing is
    # broken. "failed" means work was attempted and went wrong — a
    # download, digest, or probe failure that a retry or a fix can cure.
    # The split is what lets a payload build treat win32-arm64's missing
    # chromium as a fact while a mid-build network flake still fails it.
    action: str
    version: Optional[str] = None
    detail: Optional[str] = None

    @property
    def ok(self) -> bool:
        """Not a hard failure. An UNAVAILABLE tool is ok: absence by
        design is a recorded fact, not an error to cure — the runtime's
        resolver shows the reason when the capability is asked for."""
        return self.action != "failed"

    @property
    def provisioned(self) -> bool:
        """The tool is actually present and recorded after this result."""
        return self.action in ("kept", "adopted", "downloaded", "system")


def _binary_rel(tool: str, target: str) -> str:
    """Where each tool's binary lands, relative to its own STORE ENTRY.

    The entry directory carries the tool name, version and target
    (``node-26.7.0-linux-x64/``), so this is the layout INSIDE it —
    ``bin/node``, not ``node/bin/node``.

    Raises for a (tool, target) pair with no known layout: the pin table
    not carrying the target already refuses earlier (``pinned_file``),
    so reaching None here means the table and this map drifted — record
    nothing rather than a fact whose path is the string "None".
    """
    win = target.startswith("win32")
    ext = ".exe" if win else ""
    rel = {
        # The Windows node zip has node.exe at the root; POSIX has bin/node.
        "node": "node.exe" if win else "bin/node",
        # `npm -g --prefix` drops .cmd shims in the prefix root on Windows
        # and POSIX shims in bin/.
        "npm": "npm.cmd" if win else "bin/npm",
        "uv": f"uv{ext}",
        # PortableGit exposes cmd/git.exe; dugite-native uses bin/git.
        "git": "cmd/git.exe" if win else "bin/git",
        "gh": f"bin/gh{ext}",
        "ripgrep": f"rg{ext}",
        # One registry tarball ships every platform's binary side by side
        # (bin/agent-browser-<platform>-<arch>); the fact records the
        # HOST's native binary so running it needs no node. The JS shim
        # (bin/agent-browser.js) is deliberately NOT the fact: it would
        # drag a node dependency into a tool that has none.
        "agent-browser": f"bin/agent-browser-{target}{ext}",
        # The llama.cpp zips unpack flat: llama-server.exe sits at the
        # entry root with its DLLs (and the cudart ones from `also`)
        # beside it, which is what the Windows loader requires.
        "llamacpp-cuda": f"llama-server{ext}",
        # The `-binary` release variants unpack flat, so the driver sits at
        # the store entry's root beside its SDK library and (on Windows)
        # the cua-driver-uia.exe UIAccess worker it spawns.
        "cua-driver": f"cua-driver{ext}",
        # playwright-core's EXECUTABLE_PATHS, verbatim: the archives keep
        # their internal layout and playwright resolves these inside the
        # entry, so the fact records the same path playwright will run.
        # linux-arm64 is the non-CfT build with the old dir spelling.
        "chromium": {
            "linux-x64": "chrome-linux64/chrome",
            "linux-arm64": "chrome-linux/chrome",
            "darwin-x64": "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            "darwin-arm64": "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            "win32-x64": "chrome-win64/chrome.exe",
        }.get(target),
        "chromium-headless-shell": {
            "linux-x64": "chrome-headless-shell-linux64/chrome-headless-shell",
            "linux-arm64": "chrome-linux/headless_shell",
            "darwin-x64": "chrome-headless-shell-mac-x64/chrome-headless-shell",
            "darwin-arm64": "chrome-headless-shell-mac-arm64/chrome-headless-shell",
            "win32-x64": "chrome-headless-shell-win64/chrome-headless-shell.exe",
        }.get(target),
    }[tool]
    if rel is None:
        raise KeyError(f"{tool!r} has no known binary layout for {target}")
    return rel


def _path_dirs(tool: str, target: str) -> Optional[list[str]]:
    """PATH dirs for tools whose surface is more than the binary's dir,
    relative to the tool's own store entry.

    PortableGit needs three: bash.exe and the coreutils live outside
    cmd/. Everything else is covered by the binary's own directory.
    """
    if tool == "git" and target.startswith("win32"):
        return ["cmd", "bin", "usr/bin"]
    return None


def _fact_rel(tool: str, version: str, target: str) -> str:
    """The binary path recorded in the facts, relative to the STORE."""
    return f"{store_entry_name(tool, version, target)}/{_binary_rel(tool, target)}"


def _fact_path_dirs(
    tool: str, version: str, target: str
) -> Optional[list[str]]:
    """``path_dirs`` recorded in the facts, relative to the STORE."""
    dirs = _path_dirs(tool, target)
    if dirs is None:
        return None
    entry = store_entry_name(tool, version, target)
    return [f"{entry}/{d}" for d in dirs]


def _stage_playwright_browser(
    pin: PinnedFile, dest: Path, tmp: Path, archive_dir: Path | None = None
) -> None:
    """A playwright browser: fetch, verify, extract — and DO NOT flatten.

    Playwright resolves the executable through the archive's own top
    directory (``chrome-linux64/chrome``), so the flattening every other
    tool wants would break the only reader this entry exists for. The
    ``INSTALLATION_COMPLETE`` marker is playwright's own installed-flag
    (registry.js ``browserDirectoryToMarkerFilePath``): without it the
    registry treats the directory as a partial download and re-fetches.
    """
    archive = _fetch_verified(pin, tmp, archive_dir=archive_dir)
    _extract(archive, dest)
    (dest / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")


def _stage_archive(
    pin: PinnedFile, dest: Path, tmp: Path, archive_dir: Path | None = None
) -> None:
    """The common case: fetch, verify, extract, un-nest.

    Flattening is decided by what the archive actually CONTAINS, not by a
    per-tool list: several projects nest under a versioned top-level dir
    on one platform and unpack flat on another (uv's POSIX tarball nests,
    its Windows zip does not — a hardcoded list got that wrong).
    ``_flatten_single_dir`` no-ops unless there is exactly one top-level
    directory, so applying it unconditionally is safe.

    ``pin.also`` archives are merged into the SAME directory afterwards,
    each verified against its own digest first. They cannot go through
    ``_extract`` directly — it EMPTIES its destination by contract, which
    would delete the primary archive's files — so each unpacks into its
    own scratch directory and is then merged in. No flattening either: an
    extra exists to place files beside the primary artifact's, so
    re-rooting the tree between the two would defeat the reason they
    share an entry.

    A name collision means two archives disagree about one file's
    contents, which no reader could resolve — fail loudly instead of
    letting extraction order decide.
    """
    archive = _fetch_verified(pin, tmp, archive_dir=archive_dir)
    _extract(archive, dest)
    _flatten_single_dir(dest)
    for index, extra in enumerate(pin.also):
        extra_archive = _fetch_verified(extra, tmp, archive_dir=archive_dir)
        scratch = tmp / f"also-{index}"
        _extract(extra_archive, scratch)
        for src in sorted(scratch.rglob("*")):
            if src.is_dir():
                continue
            target = dest / src.relative_to(scratch)
            if target.exists():
                raise RuntimeError(
                    f"{extra.filename} would overwrite {target.name} from "
                    f"{pin.filename}: the two pinned archives disagree about "
                    f"one file, which extraction order must not decide"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))


def _stage_portable_git(
    pin: PinnedFile, dest: Path, tmp: Path, archive_dir: Path | None = None
) -> None:
    """PortableGit is a self-extracting 7z, not an archive we can read.

    It is the one asset that must be EXECUTED to unpack, so the digest
    check matters more here than anywhere else — ``_fetch_verified`` has
    already proven the bytes before this runs it.
    """
    sfx = _fetch_verified(pin, tmp, archive_dir=archive_dir)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    sfx.chmod(0o755)
    proc = subprocess.run(
        [str(sfx), f"-o{dest}", "-y"], capture_output=True, timeout=900
    )
    if proc.returncode != 0:
        raise RuntimeError(f"PortableGit self-extractor exited {proc.returncode}")


def _stage_npm(
    pin: PinnedFile,
    dest: Path,
    tmp: Path,
    ctx: "_StageContext",
    target: str,
    archive_dir: Path | None = None,
) -> None:
    """npm installs itself, using the node it extends.

    npm is not a relocatable archive: its own ``bin/npm`` resolves the cli
    from ``dirname(process.execPath)``, so a plain unpack on PATH finds
    the npm BUNDLED inside node and fails outright. Letting npm do a
    global install into a prefix produces the launchers each platform
    actually needs (POSIX symlinks in ``bin/``, ``.cmd``/``.ps1`` shims in
    the prefix root) instead of us hand-writing shims per OS.

    The bytes are still the pinned, digest-verified tarball —
    ``--offline`` guarantees the registry is never consulted, so this
    installs exactly what the pin table says and nothing else.
    """
    tarball = _fetch_verified(pin, tmp, archive_dir=archive_dir)
    node = ctx.node
    if node is None or not node.is_file():
        raise RuntimeError("npm extends node, which is not provisioned")

    # node's BUNDLED npm performs the install; the pinned npm replaces it
    # on PATH afterwards. Driving npm-cli.js through node directly avoids
    # depending on any npm shim already being resolvable.
    bundled_cli = (
        node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if target.startswith("win32")
        else node.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    )
    if not bundled_cli.is_file():
        raise RuntimeError(f"node ships no bundled npm at {bundled_cli}")

    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            str(node),
            str(bundled_cli),
            "install",
            "--global",
            "--prefix",
            str(dest),
            "--offline",
            "--no-audit",
            "--no-fund",
            str(tarball),
        ],
        capture_output=True,
        text=True,
        timeout=900,
        # Keep the install off the user's ~/.npm: an install-scoped tool
        # writes install-scoped state.
        env={**os.environ, "npm_config_cache": str(ctx.npm_cache)},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"npm install exited {proc.returncode}: {proc.stderr[-400:]}")


@dataclass(frozen=True)
class _StageContext:
    """What a staging routine may need beyond its own archive.

    Only npm needs either today (it is unpacked BY the node it extends),
    but passing them explicitly is what lets every other routine stay a
    pure archive → directory function with no idea where the store is.
    """

    node: Optional[Path]  # the provisioned node binary, resolved from facts
    npm_cache: Path  # install-scoped npm cache (mutable state, not bytes)


def _prune_foreign_agent_browser_binaries(dest: Path, target: str) -> None:
    """Drop every agent-browser binary that is not this target's.

    One registry tarball ships all seven platform binaries side by side
    (~69MB of bin/), and a sealed payload runs exactly one of them --
    ``_binary_rel`` names it. The other six are bytes no code path on
    this machine can reach: the fact records the native binary directly,
    so the JS shim that would select among them at runtime is never the
    entry point.

    They are not merely dead weight. A wrong-arch binary inside a packed
    desktop artifact is what ``audit-bundle-arch.mjs`` exists to catch,
    and exempting them there would blind the audit to a genuinely
    mis-staged driver -- the same reasoning that keeps dugite-native out
    of the audit's PortableGit exemption. Deleting them keeps the audit
    honest instead of teaching it to look away.

    Keeps the JS shim and the sidecars: they are kilobytes, they carry no
    architecture, and they are what an operator reads when debugging
    which binary the upstream package would have picked.
    """
    bin_dir = dest / "bin"
    if not bin_dir.is_dir():
        raise RuntimeError(
            f"agent-browser staged without a bin/ directory in {dest}"
        )
    keep = Path(_binary_rel("agent-browser", target)).name
    if not (bin_dir / keep).is_file():
        raise RuntimeError(
            f"agent-browser: {keep} missing from the staged tarball -- "
            "the upstream bin/ layout changed and the pruner would delete "
            "every binary"
        )
    for item in bin_dir.iterdir():
        if item.is_file() and item.name.startswith("agent-browser-") and item.name != keep:
            item.unlink()


def _prune_cua_driver_sdk_artifacts(dest: Path) -> None:
    """Drop the embedding SDK that ships beside the cua-driver CLI.

    The release tarball carries an SDK for programs that EMBED the driver
    as a library: ``libcua_driver_sdk.{so,dylib}`` / ``cua_driver_sdk.dll``
    (~38-47MB), the Node addon ``cua_driver_node_runtime.node``, and the C
    header ``cua_driver_abi.h``. Hermes is not such a program — it speaks
    MCP over stdio to the CLI — and the CLI does not use them either: the
    binary neither links the library (no DT_NEEDED entry) nor names it for
    a dlopen, verified against the 0.21.0 linux-x64 build.

    So they are bytes no code path on this machine can reach, and in a
    sealed desktop payload they are bytes every user downloads. Same call
    the staging already makes for dugite's Windows-only DLLs on POSIX and
    for agent-browser's foreign-arch binaries.

    ``cua-cursor-theme`` and ``wayland-helper/`` stay: the driver
    references the cursor theme by name, and the Wayland helper is the
    GNOME extension a Linux user installs for window rects.

    Raises when the CLI itself is missing rather than pruning blind — an
    upstream layout change must fail the build, not silently ship a
    payload whose driver was deleted.
    """
    if not (dest / "cua-driver").is_file() and not (dest / "cua-driver.exe").is_file():
        raise RuntimeError(
            f"cua-driver: no CLI binary in the staged tarball at {dest} — "
            "the upstream layout changed and the pruner would delete the "
            "wrong files"
        )
    for name in (
        "libcua_driver_sdk.so",
        "libcua_driver_sdk.dylib",
        "cua_driver_sdk.dll",
        "cua_driver_node_runtime.node",
        "cua_driver_abi.h",
    ):
        (dest / name).unlink(missing_ok=True)


def _stage(
    tool: str,
    pin: PinnedFile,
    dest: Path,
    tmp: Path,
    target: str,
    ctx: _StageContext,
    archive_dir: Path | None = None,
) -> None:
    """Unpack one tool into *dest* — a scratch dir, not its final home.

    Branching lives here and nowhere else: every tool arrives through the
    same fetch-and-verify, and differs only in how its artifact unpacks.
    The caller publishes *dest* into the store with one atomic rename.
    """
    if tool == "git" and target.startswith("win32"):
        _stage_portable_git(pin, dest, tmp, archive_dir=archive_dir)
        return

    if tool == "npm":
        _stage_npm(pin, dest, tmp, ctx, target, archive_dir=archive_dir)
        return

    if tool in PLAYWRIGHT_BROWSER_TOOLS:
        _stage_playwright_browser(pin, dest, tmp, archive_dir=archive_dir)
        return

    _stage_archive(pin, dest, tmp, archive_dir=archive_dir)
    if tool == "agent-browser":
        _prune_foreign_agent_browser_binaries(dest, target)
    if tool == "cua-driver":
        _prune_cua_driver_sdk_artifacts(dest)
    if tool == "git" and not target.startswith("win32"):
        # dugite ships Windows remote-helper DLLs in every build. They
        # are dead weight on POSIX (~40MB) and now cost store space that
        # several installs share.
        for dll_file in (dest / "libexec" / "git-core").rglob("*.dll"):
            dll_file.unlink()


# ─── system-tool acceptance (decision 1: system-git-first) ─────────────────
#
# The floor is DERIVED FROM THE FLAGS THIS CODEBASE USES, not chosen by
# taste. scripts/audit-git-flags.py extracts every git argv we build; the
# newest-introduced flag sets the floor, because a system git older than
# that would accept the probe and then fail mid-update on a real call.
#
# The audit's nontrivial finds (everything else predates 2.13):
#
#   flag                                introduced   used at
#   ----                                ----------   -------
#   stash push -u -m                    2.13         update_cmd.py:1349
#   stash list --format=%gd %H          2.13*        update_cmd.py:1414
#   rev-parse --path-format=absolute    2.31         kanban_db.py:7513
#   branch --show-current               2.22         kanban_db.py:7551
#   push --force-with-lease             1.8.5        update_cmd.py:1748
#   diff --diff-filter=U                1.6-era      update_cmd.py:1512
#   -c windows.appendAtomically         GfW-only†    update_cmd.py:4444
#
#   *  `git stash push` (the subcommand form) is the 2.13 item; the
#      --format string itself is old reflog syntax.
#   †  windows.appendAtomically exists ONLY in Git for Windows (their
#      Documentation/config/windows.adoc; never upstreamed). Mainline
#      git tolerates the unknown key — `-c foo.bar=x` only errors on a
#      MALFORMED key, not an unknown one — and the sites that set it are
#      win32-gated anyway, so it does not raise the floor. It DOES mean
#      a macOS/Linux git can never satisfy a Windows install's needs,
#      which is already true for bash.exe reasons.
#
# Floor: 2.31 (rev-parse --path-format=absolute, the newest). The value
# lives in installation.git with the locator that enforces it, imported
# at the top of this module — one number rather than two that can drift.


def probe_system_git() -> Optional[tuple[str, str]]:
    """A usable machine-provided git: ``(absolute_path, version)`` or None.

    Delegates to :mod:`installation.git`, which owns the posture: not
    the macOS xcode-select shim, meets the flag floor, and POSIX-only
    because Windows always takes the managed PortableGit (bash.exe
    ships inside it).
    """
    from installation.git import probe_system_git as _probe

    return _probe()


# ─── the provisioning loop ──────────────────────────────────────────────────


def _discard_scratch(scratch: Path) -> None:
    """Delete a provisioning scratch dir, and shrug when the OS says no.

    A scratch file we cannot delete is not a provisioning failure: by
    the time this runs the tool is already unpacked into the runtime
    dir, and the OS reclaims its own temp dir later. On Windows the
    deleter races whatever still holds the artifact open — the
    PortableGit self-extractor outlives its own exit, and Defender
    cannot be disabled on the windows-11-arm image, so it scans the
    downloaded .exe and holds it too. Both surface as WinError 5, which
    used to abort the whole tool AFTER it had been staged.
    """
    # ignore_errors, not onerror/onexc: the callback spelling changed in
    # 3.12 and the deprecated one is removed in 3.14, and nothing here
    # needs the per-file exception — only whether anything survived.
    shutil.rmtree(scratch, ignore_errors=True)
    if scratch.exists():
        logger.debug("scratch dir %s could not be removed — leaving it", scratch)


# Written INSIDE a store entry, in the staged tree, immediately before the
# entry is published. Its presence is what makes "this entry is the pinned,
# digest-verified artifact" a fact a later run can CHECK rather than assume.
#
# It has to be inside the tree, not beside it: the publish is a single
# rename, so a marker that lands with the tree cannot be separated from it
# by a crash. A marker written after the rename would leave a window where
# a complete entry looks like junk.
ENTRY_MARKER_NAME = ".hermes-store-entry.json"


def _entry_marker(pin: PinnedFile, tool: str, target: str) -> dict:
    marker = {
        "tool": tool,
        "version": pin.version,
        "target": target,
        "sha256": pin.sha256,
        "publishedAt": datetime.now(timezone.utc).isoformat(),
    }
    if pin.also:
        # An entry assembled from several archives is only the entry the
        # pin describes if EVERY archive is accounted for; recording just
        # the primary digest would let a half-assembled tree pass as
        # published.
        marker["alsoSha256"] = [extra.sha256 for extra in pin.also]
    return marker


def _is_published_entry(entry: Path, tool: str, version: str, target: str) -> bool:
    """True when *entry* is an entry THIS code published for this tuple.

    Anything else — a hand-made directory, a tree left by an interrupted
    delete, an unpacked copy someone dropped in — is not trusted. That is
    the no-salvage rule surviving the move to a shared store: adopting
    bytes nobody verified would defeat pinning digests at all.
    """
    try:
        marker = json.loads((entry / ENTRY_MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        marker.get("tool") == tool
        and marker.get("version") == version
        and marker.get("target") == target
    )


def _replace_with_retry(
    staged: Path,
    entry: Path,
    *,
    is_windows: bool | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """``os.replace`` with a bounded retry for Windows scanner races.

    On Windows a directory rename fails with access-denied (winerror 5)
    or sharing-violation (winerror 32) while ANY file inside is held
    open — and Defender/the search indexer scan freshly-extracted trees.
    PortableGit's thousands of files make git the reliable loser of that
    race (observed on the win32-arm64 payload lane). The hold is
    transient: retry with backoff for ~15s total before giving up.
    Non-Windows takes the first call, and a non-transient error (real
    permissions, wrong filesystem) still raises immediately.

    *is_windows* and *sleep* are data/injection points so the retry
    policy is testable on any host (AGENTS.md: platform as data, not a
    faked host).
    """
    if is_windows is None:
        is_windows = sys.platform == "win32"
    delay = 0.5
    for attempt in range(6):
        try:
            os.replace(staged, entry)
            return
        except OSError as exc:
            transient = is_windows and getattr(exc, "winerror", None) in (5, 32)
            if not transient or attempt == 5:
                raise
            logger.debug(
                "publish rename blocked (winerror %s), retry %d in %.1fs",
                getattr(exc, "winerror", None), attempt + 1, delay,
            )
            sleep(delay)
            delay *= 2


def _publish(staged: Path, entry: Path, tool: str, version: str, target: str) -> bool:
    """Move a staged tree into the store under its final name.

    Returns True when this call published the entry, False when a
    published entry was already there. Both are success: the marker
    proves the existing entry is the same verified artifact, so keeping
    it is correct AND required — another install may be executing it
    right now, and a published entry is never rewritten.

    An UNPUBLISHED directory at the same name is junk (no marker, so no
    process can be relying on it) and is replaced.

    The rename is the whole concurrency story. Extraction happens in a
    scratch dir, so a reader of the store never sees a partial tree.
    """
    entry.parent.mkdir(parents=True, exist_ok=True)
    if entry.exists():
        if _is_published_entry(entry, tool, version, target):
            return False
        shutil.rmtree(entry, ignore_errors=True)
    try:
        _replace_with_retry(staged, entry)
    except OSError:
        # Lost the race between the check and the rename, or the platform
        # refuses to replace a directory (Windows raises for a non-empty
        # target). A published entry there now is the outcome we wanted.
        if _is_published_entry(entry, tool, version, target):
            return False
        raise
    return True


def _provision_one(
    tool: str,
    entry: dict,
    facts_dir: Path,
    store: Path,
    facts: dict[str, RuntimeFact],
    target: str,
    path_order: list[str] | None = None,
    archive_dir: Path | None = None,
) -> ToolResult:
    """Bring ONE tool to the pinned state. Never raises."""
    # A DECLARED gap ({\"missing\": reason} in the pin table) must be
    # detected BEFORE anything derives a binary layout from (tool,
    # target): _binary_rel has no row for a target the pin table refuses,
    # and raising there would turn a designed absence into a crash
    # instead of the recorded "unavailable" fact the resolver reads.
    try:
        pinned_file(tool, target, pins={tool: entry})
    except UnavailableOnTarget as exc:
        return ToolResult(tool, "unavailable", detail=exc.reason)

    version = entry["version"]
    rel = _fact_rel(tool, version, target)
    store_entry = store / store_entry_name(tool, version, target)
    published = _is_published_entry(store_entry, tool, version, target)

    # Already exactly right? The pin is exact, so this is an equality
    # check, not a range check. The store may also hold the entry from
    # ANOTHER install, in which case this install only needs the fact —
    # that is the sharing this store exists for, and it costs no download.
    fact = facts.get(tool)
    if (
        fact is not None
        and fact.version == version
        and published
        and (store / rel).is_file()
    ):
        return ToolResult(tool, "kept", version=version)

    # System-git-first (decision 1): a machine git that clears the flag
    # floor beats a 147MB download. The fact records the absolute path
    # with source="system", and a still-valid system fact is `kept` on
    # every later sweep. If the binary vanished (uninstall) or now fails
    # the floor (downgrade — rare but real on distro rollbacks), fall
    # through to the pinned download rather than limping.
    #
    # NEVER for a self-contained runtime dir (facts and bytes in one
    # directory — resolve_bases' packager case). That artifact ships to
    # OTHER machines: a system fact would record this build runner's
    # absolute git path into the payload, and the desktop's arch gate
    # rightly rejects it. A sealed payload carries its own git, always.
    if tool == "git" and facts_dir != store:
        if (
            fact is not None
            and fact.source == "system"
            and Path(fact.path).is_file()
            and probe_system_git() is not None
        ):
            return ToolResult(tool, "kept", version=fact.version)
        if fact is None or fact.source == "system":
            system = probe_system_git()
            if system is not None:
                path, sys_version = system
                facts[tool] = RuntimeFact(
                    version=sys_version, path=path, source="system"
                )
                save_facts(facts, facts_dir, path_order=path_order)
                return ToolResult(tool, "system", version=sys_version)

    try:
        pin = pinned_file(tool, target, pins={tool: entry})
    except KeyError as exc:
        return ToolResult(tool, "failed", detail=str(exc))

    # When the store already holds the published entry, this run only
    # writes the FACT — the bytes were fetched by whoever published
    # (another install, or the installer's bootstrap staging). Worth
    # telling apart in the receipt: "downloaded" claims network traffic
    # that never happened, and the 44-worktree case this store exists
    # for should be visible as adoption, not as 44 downloads.
    adopted = published and (store / rel).is_file()

    try:
        if not adopted:
            ctx = _StageContext(
                node=(
                    store / _fact_rel("node", facts["node"].version, target)
                    if "node" in facts
                    else None
                ),
                npm_cache=facts_dir / "cache" / "npm",
            )
            td = Path(tempfile.mkdtemp(prefix="hermes-provision-"))
            try:
                # Stage into the STORE's own scratch area, not the OS temp
                # dir: publishing is an os.replace, which fails across
                # filesystems, and /tmp is very often a different one.
                staging = store / f".staging-{uuid.uuid4().hex}"
                _stage(
                    tool, pin, staging, Path(td), target, ctx, archive_dir=archive_dir
                )
                if not (staging / _binary_rel(tool, target)).is_file():
                    shutil.rmtree(staging, ignore_errors=True)
                    return ToolResult(
                        tool, "failed", detail=f"{rel} missing after staging"
                    )
                # The marker goes in BEFORE the rename, so it lands with
                # the tree in one atomic step (see ENTRY_MARKER_NAME).
                (staging / ENTRY_MARKER_NAME).write_text(
                    json.dumps(_entry_marker(pin, tool, target)), encoding="utf-8"
                )
                if not _publish(staging, store_entry, tool, version, target):
                    # Someone else published the same entry while we
                    # staged. Their bytes pass the same digest check, so
                    # drop ours rather than touching a live entry.
                    shutil.rmtree(staging, ignore_errors=True)
            finally:
                _discard_scratch(td)

        binary = store / rel
        if not binary.is_file():
            return ToolResult(tool, "failed", detail=f"{rel} missing after staging")
        binary.chmod(binary.stat().st_mode | 0o755)

        # Verify by RUNNING it, not by trusting the archive: a cross-arch
        # or half-extracted binary fails here rather than at first use.
        probe_env = _probe_env(entry, facts_dir, store, tool)
        if _probe_version(binary, env=probe_env) is None:
            return ToolResult(tool, "failed", detail="provisioned binary does not run")
        # A binary whose version came out of its PE resource was never
        # executed, so it has not been verified yet — the file read
        # cannot tell a working browser from a cross-arch one. Render a
        # page to close that gap.
        if sys.platform == "win32" and _is_gui_chrome(binary):
            if not _renders_a_page(binary, env=probe_env):
                return ToolResult(
                    tool, "failed", detail="provisioned browser does not render"
                )

        facts[tool] = RuntimeFact(
            version=version,
            path=rel,
            path_dirs=_fact_path_dirs(tool, version, target),
        )
        save_facts(facts, facts_dir, path_order=path_order)
        return ToolResult(tool, "adopted" if adopted else "downloaded", version=version)
    except Exception as exc:  # noqa: BLE001 — per-tool isolation is the contract
        logger.warning("provisioning %s failed: %s", tool, exc)
        return ToolResult(tool, "failed", detail=str(exc))


def provision_tool(
    tool: str,
    runtime_dir: Path | None = None,
    install_root: Path | None = None,
    store_dir: Path | None = None,
) -> ToolResult:
    """Provision a single pinned tool, and whatever it extends.

    Used by the self-heal paths that need exactly one runtime (the
    managed-Node bootstrap) without paying for a full sweep, and by the
    on-demand path for OPTIONAL tools — a capability nobody asked for is
    not downloaded until something asks.

    A tool is staged by RUNNING what it extends (an npm package needs the
    provisioned node and npm), so the chain is brought up first. Each
    dependency goes through the same ``kept`` fast path, so this is a
    no-op when they are already at their pins.
    """
    facts_dir, store = resolve_bases(runtime_dir, store_dir)
    facts_dir.mkdir(parents=True, exist_ok=True)
    store.mkdir(parents=True, exist_ok=True)
    pins = load_pins(install_root)
    entry = pins.get(tool)
    if entry is None:
        return ToolResult(tool, "failed", detail=f"{tool} is not pinned")

    facts = load_facts(facts_dir)
    target = current_target()
    order = path_order(pins)
    # install_order is the full dependency-respecting order; keep the
    # prefix this tool actually needs — itself, what it transitively
    # extends (staging may RUN those), and what it requires (a driver
    # without its browser is not a state the lazy path may produce
    # either), with each required tool's own extends chain in turn.
    needed: set[str] = set()
    for name in requires_closure(tool, pins):
        needed |= extends_closure(name, pins)
    chain = [name for name in install_order(pins) if name in needed]

    result = ToolResult(tool, "failed", detail=f"{tool} was not provisioned")
    for name in chain:
        result = _provision_one(
            name, pins[name], facts_dir, store, facts, target, path_order=order
        )
        if not result.provisioned:
            # A dependency that cannot be staged — broken OR absent by
            # design — makes the request impossible; report the result
            # that actually stopped the chain.
            return result
    return result


def provision_runtimes(
    runtime_dir: Path | None = None,
    install_root: Path | None = None,
    emit: Callable[[dict], None] | None = None,
    store_dir: Path | None = None,
    archive_dir: Path | None = None,
    extras: list[str] | None = None,
) -> list[ToolResult]:
    """Bring every pinned tool to its pinned version.

    Never raises for a single tool — each failure is recorded and the
    rest proceed (a broken ripgrep download must not kill node).

    Tools are provisioned in the pin table's dependency order, so a tool
    that extends another is staged after it — npm is unpacked by running
    the node it extends, which has to exist first.

    Two selections, and they answer different questions:

    * default — every REQUIRED tool, plus optional tools this install has
      already recorded (that is what carries a pin bump onto an install
      which uses the capability).
    * *extras* — the default sweep PLUS these optional tools by name,
      each expanded through its ``requires`` closure (a driver without
      the browser it launches is not a state anyone wants to ask for).
      For a caller that wants a full install and some capabilities with
      it: the desktop payload and the install scripts.

    An unknown name raises ``ValueError`` rather than silently matching
    nothing — a typo must not look like a successful run that
    provisioned nothing.

    Provisioning is always for THIS host. A tool is never recorded until
    the staged binary has answered a version probe here, so a pin that
    downloads but cannot run is a failure rather than a fact.

    *archive_dir* names a pin-archive cache of verified downloads (see
    ``_fetch_verified``): only the desktop payload staging passes it, so
    a normal install never grows one.
    """
    facts_dir, store = resolve_bases(runtime_dir, store_dir)
    facts_dir.mkdir(parents=True, exist_ok=True)
    store.mkdir(parents=True, exist_ok=True)
    target = current_target()
    pins = load_pins(install_root)
    facts = load_facts(facts_dir)
    results: list[ToolResult] = []
    order = path_order(pins)

    extra = set(extras or ())
    unknown = sorted(extra - set(pins))
    if unknown:
        # A name that matches nothing would otherwise skip the loop body
        # entirely and report success having provisioned nothing — the
        # exact silent-empty-payload outcome this engine exists to avoid.
        raise ValueError(
            f"provision_runtimes: not in the pin table: {', '.join(unknown)}"
        )
    # Expand each requested extra through its requires closure BEFORE the
    # sweep: --extra agent-browser must stage the chromium pair it
    # launches, not just the driver (selection only — order and PATH stay
    # the pin table's business).
    for name in list(extra):
        extra |= requires_closure(name, pins)

    for tool in install_order(pins):
        if is_optional(tool, pins) and tool not in facts and tool not in extra:
            # An optional tool is provisioned on demand (provision_tool or
            # an explicit --extra request), not for everyone. Once the facts
            # record it, though, the sweep owns it like any other tool.
            continue
        result = _provision_one(
            tool,
            pins[tool],
            facts_dir,
            store,
            facts,
            target,
            path_order=order,
            archive_dir=archive_dir,
        )
        results.append(result)
        if emit:
            emit(
                {
                    "type": "runtime-tool",
                    "tool": result.tool,
                    "action": result.action,
                    "version": result.version,
                    "detail": result.detail,
                }
            )

    return results


def stale_tools(
    runtime_dir: Path | None = None,
    install_root: Path | None = None,
    store_dir: Path | None = None,
) -> dict[str, tuple[str, Optional[str]]]:
    """Pinned tools whose installed state does not match the pin table.

    Maps tool → (pinned version, installed version or None). Empty means
    every pin is satisfied. This is the same equality check
    ``_provision_one`` makes before deciding to re-download — exact pins
    make it an equality check, not a range check.

    An OPTIONAL tool that was never installed is not stale: it is a
    capability nobody asked for, and reporting it as drift would make
    every install that does not browse look broken. Once it IS installed
    the pin governs it like any other tool, so a bump shows up here.
    """
    facts_dir, store = resolve_bases(runtime_dir, store_dir)
    facts = load_facts(facts_dir)
    drift: dict[str, tuple[str, Optional[str]]] = {}

    pins = load_pins(install_root)
    for tool, entry in pins.items():
        fact = facts.get(tool)
        installed = fact.version if fact is not None else None
        # A system-provided tool is deliberately NOT at the pinned
        # version — it satisfied the flag floor instead (decision 1's
        # third state). Reporting it as drift would
        # make every install using its distro git look permanently
        # broken. Only a vanished binary turns it back into work.
        if fact is not None and fact.source == "system":
            if Path(fact.path).is_file():
                continue
            installed = None
        # The FACT's own path, not a recomputed one: a fact recorded at an
        # older pin names an older store entry, and asking whether THAT
        # is on disk is the question ("is what we recorded still there?").
        elif fact is not None and not (store / fact.path).is_file():
            # Recorded but vanished reads as unprovisioned everywhere
            # else; say so here too rather than reporting it as current.
            installed = None
        if installed is None and is_optional(tool, pins):
            continue
        if installed != entry["version"]:
            drift[tool] = (entry["version"], installed)
    return drift


class StaleManagedRuntimes(RuntimeError):
    """A sealed install's runtime tools disagree with its pin table."""


def require_current_runtimes(
    project_root: Path | None = None,
    runtime_dir: Path | None = None,
    install_root: Path | None = None,
    store_dir: Path | None = None,
) -> None:
    """Fail fast when a SEALED install ships out-of-date runtime tools.

    A git checkout provisions on demand: drift there is a normal state
    that the next `hermes update` (or the self-heal path) resolves, and
    raising would break the very run that fixes it.

    A sealed tree cannot self-heal. Its steward — Nix, Docker, the
    desktop bundle — builds the runtime tools as part of the artifact, so
    drift means the artifact was assembled against a different pin table
    than the code it ships. Every consequence of that is worse and more
    confusing than stopping here: tools silently missing from PATH,
    or a version the code does not expect. The steward has to rebuild.
    """
    root = project_root if project_root is not None else get_install_root()
    tree = runtime_tree(root)
    if not isinstance(tree, Sealed):
        return

    drift = stale_tools(
        runtime_dir=runtime_dir, install_root=install_root, store_dir=store_dir
    )
    if not drift:
        return

    lines = [
        f"  {tool}: pinned {pinned}, installed {installed or 'nothing'}"
        for tool, (pinned, installed) in sorted(drift.items())
    ]
    raise StaleManagedRuntimes(
        f"This Hermes is a sealed install managed by {tree.steward!r}, and its "
        "managed runtime tools do not match runtime-pins.json:\n"
        + "\n".join(lines)
        + "\n\nThe artifact was built against a different pin table than the code "
        "it ships. Rebuild it with its steward — a sealed tree cannot provision "
        "these itself."
    )


def step_provision_runtimes() -> dict:
    """post_update MACHINE_STEPS entry."""
    results = provision_runtimes()
    failed = [r for r in results if not r.ok]
    return {
        "ok": not failed,
        "tools": {r.tool: r.action for r in results},
        **(
            {"error": "; ".join(f"{r.tool}: {r.detail}" for r in failed)}
            if failed
            else {}
        ),
    }


def main(argv: list[str] | None = None) -> int:
    """``python -m installation.provisioner`` — provision into a dir.

    The desktop payload staging shells out to this rather than carrying a
    second implementation of download-and-verify in JavaScript.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m installation.provisioner")
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="Where the FACTS go (default: this install's .hermes-runtime). "
        "Naming it without --store-dir also makes it the byte store, which "
        "is what a packager building one self-contained runtime dir wants.",
    )
    parser.add_argument(
        "--store-dir",
        type=Path,
        help="Where the tool BYTES go (default: ~/.hermes/tools, shared by "
        "every install on this machine).",
    )
    parser.add_argument(
        "--target",
        help="Assert this host IS this pin target, e.g. darwin-arm64. "
        "Provisioning is always for this host; the flag lets a caller "
        "state the target it believes it is on instead of inferring it. "
        "A mismatch exits 2.",
    )
    parser.add_argument(
        "--extra",
        dest="extras",
        action="append",
        metavar="TOOL",
        help="Provision the default sweep PLUS this optional tool "
        "(repeatable: --extra git --extra agent-browser), expanded through "
        "the pin table's 'requires' edges. "
        "What a payload build wants: every required tool, "
        "and the capabilities this artifact ships with. A target the pin "
        "table declares no build for is reported 'unavailable' and does "
        "not fail the run, so the caller never has to filter by platform.",
    )
    parser.add_argument(
        "--archive-cache",
        type=Path,
        default=None,
        help="A build cache of verified downloads keyed <sha256>-<filename>. "
        "A hit skips the network but never the digest check, and entries no "
        "pin references any more are pruned. Only the desktop payload "
        "staging passes this — a normal install never grows one.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON lines.")
    ns = parser.parse_args(argv)

    # An asserted target that is not this host means the caller is wrong about
    # the machine it is on. Staging pins for another platform would write
    # binaries that cannot be probed here, so refuse rather than record facts
    # no one verified.
    host = current_target()
    if ns.target and ns.target != host:
        print(
            f"runtime_provisioner: --target {ns.target} is not this host ({host})",
            file=sys.stderr,
        )
        return 2

    def emit(event: dict) -> None:
        if ns.json:
            print(json.dumps(event), flush=True)
        else:
            version = f" {event['version']}" if event.get("version") else ""
            detail = f" — {event['detail']}" if event.get("detail") else ""
            print(f"  {event['tool']}: {event['action']}{version}{detail}", flush=True)

    if ns.archive_cache is not None:
        # Prune BEFORE provisioning: entries whose digest left the pin
        # table are dead weight, and a crashed writer's orphaned .tmp-*
        # files go with them. Live entries survive to serve this run.
        prune_archive_cache(ns.archive_cache, load_pins())

    try:
        results = provision_runtimes(
            runtime_dir=ns.runtime_dir,
            store_dir=ns.store_dir,
            emit=emit,
            extras=ns.extras,
            archive_dir=ns.archive_cache,
        )
    except ValueError as exc:
        # A bad selection (a name no pin table row matches). Exit 2 like
        # the target mismatch above: the caller asked for something
        # impossible, which is not the same as a tool failing.
        print(f"runtime_provisioner: {exc}", file=sys.stderr)
        return 2
    # Exit 1 only for HARD failures. An "unavailable" tool is a declared
    # gap the pin table states on purpose (win32-arm64 has no chromium
    # build) — the receipt line above says so, and failing the build over
    # it would force every caller to pre-filter tools per target,
    # duplicating knowledge the table already owns.
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
