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
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from installation.paths import get_install_root, resolve_bases
from installation.tree import Sealed, runtime_tree
from installation.registry import (
    PinnedFile,
    RuntimeFact,
    current_target,
    extends_closure,
    install_order,
    is_optional,
    load_facts,
    load_pins,
    path_order,
    pinned_file,
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


def _fetch_verified(pin: PinnedFile, into: Path) -> Path:
    """Download a pinned artifact and prove it is the pinned bytes.

    The digest check happens BEFORE anything is unpacked or executed: a
    mismatched archive is deleted, never extracted. This is the only
    thing standing between a compromised CDN and a user's machine.
    """
    archive = into / pin.filename
    _download(pin.url, archive)

    actual = _sha256(archive)
    if actual != pin.sha256:
        archive.unlink(missing_ok=True)
        raise RuntimeError(
            f"sha256 mismatch for {pin.filename}: "
            f"pinned {pin.sha256}, downloaded {actual}"
        )
    return archive


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
            for info in zf.infolist():
                # extract() RETURNS the path it actually wrote, with the
                # entry name already sanitized (".." stripped, absolute
                # paths made relative). Chmod that, never info.filename:
                # an entry named "../../victim" chmods a file OUTSIDE the
                # destination, which is an arbitrary chmod +x for anyone
                # who can serve us an archive.
                written = Path(zf.extract(info, dest))
                mode = info.external_attr >> 16
                if mode & 0o111 and written.is_file():
                    written.chmod(mode & 0o777)
    else:
        raise ValueError(f"unsupported archive: {archive.name}")


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
    binary: Path, args: list[str] | None = None, env: dict[str, str] | None = None
) -> Optional[str]:
    """Run `<binary> --version` and return the first version-shaped token.

    None when the binary does not run — callers treat that as
    unprovisioned, never as fatal.
    """
    try:
        out = subprocess.run(
            [str(binary)] + (args or ["--version"]),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    import re as _re

    m = _re.search(r"\d+(?:\.\d+)+", out or "")
    return m.group(0) if m else None


def _probe_env(
    entry: dict, facts_dir: Path, store: Path
) -> Optional[dict[str, str]]:
    """Environment for the run-the-binary check.

    Most tools are self-contained executables and need nothing. A tool
    that extends another is a script launched by it — npm's shim is
    ``#!/usr/bin/env node`` — so the probe has to see the runtime dir's
    own tools on PATH, or it reports "does not run" on any host without a
    system copy and the tool is never recorded.
    """
    if not entry.get("extends"):
        return None
    from installation.env import with_managed_runtimes

    return with_managed_runtimes(runtime_dir=facts_dir, store_dir=store)


# ─── per-tool layout + staging ──────────────────────────────────────────────


@dataclass
class ToolResult:
    tool: str
    action: str  # kept | adopted | downloaded | system | failed
    version: Optional[str] = None
    detail: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.action != "failed"


def _binary_rel(tool: str, target: str) -> str:
    """Where each tool's binary lands, relative to its own STORE ENTRY.

    The entry directory carries the tool name, version and target
    (``node-26.7.0-linux-x64/``), so this is the layout INSIDE it —
    ``bin/node``, not ``node/bin/node``.
    """
    win = target.startswith("win32")
    ext = ".exe" if win else ""
    return {
        # The Windows node zip has node.exe at the root; POSIX has bin/node.
        "node": "node.exe" if win else "bin/node",
        # `npm -g --prefix` drops .cmd shims in the prefix root on Windows
        # and POSIX shims in bin/ (same split dep_ensure documents).
        "npm": "npm.cmd" if win else "bin/npm",
        "uv": f"uv{ext}",
        # PortableGit exposes cmd/git.exe; dugite-native uses bin/git.
        "git": "cmd/git.exe" if win else "bin/git",
        "gh": f"bin/gh{ext}",
        "ripgrep": f"rg{ext}",
        # camoufox-js's own LAUNCH_FILE map: the zip unpacks flat and the
        # launcher sits at its root (mac keeps the .app bundle layout).
        "camoufox": (
            "camoufox.exe"
            if win
            else (
                "Camoufox.app/Contents/MacOS/camoufox"
                if target.startswith("darwin")
                else "camoufox-bin"
            )
        ),
    }[tool]


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


def _stage_archive(pin: PinnedFile, dest: Path, tmp: Path) -> None:
    """The common case: fetch, verify, extract, un-nest.

    Flattening is decided by what the archive actually CONTAINS, not by a
    per-tool list: several projects nest under a versioned top-level dir
    on one platform and unpack flat on another (uv's POSIX tarball nests,
    its Windows zip does not — a hardcoded list got that wrong).
    ``_flatten_single_dir`` no-ops unless there is exactly one top-level
    directory, so applying it unconditionally is safe.
    """
    archive = _fetch_verified(pin, tmp)
    _extract(archive, dest)
    _flatten_single_dir(dest)


def _stage_portable_git(pin: PinnedFile, dest: Path, tmp: Path) -> None:
    """PortableGit is a self-extracting 7z, not an archive we can read.

    It is the one asset that must be EXECUTED to unpack, so the digest
    check matters more here than anywhere else — ``_fetch_verified`` has
    already proven the bytes before this runs it.
    """
    sfx = _fetch_verified(pin, tmp)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    sfx.chmod(0o755)
    proc = subprocess.run(
        [str(sfx), f"-o{dest}", "-y"], capture_output=True, timeout=900
    )
    if proc.returncode != 0:
        raise RuntimeError(f"PortableGit self-extractor exited {proc.returncode}")


def _stage_npm(
    pin: PinnedFile, dest: Path, tmp: Path, ctx: "_StageContext", target: str
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
    tarball = _fetch_verified(pin, tmp)
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


def _camoufox_version_json(pinned_version: str) -> dict[str, str]:
    """Split a pin like ``152.0.4-beta.28`` the way camoufox-js does.

    Its fetcher matches ``camoufox-(.+)-(.+)-<os>.<arch>.zip`` GREEDILY
    against the asset name and builds ``Version(match[2], match[1])`` —
    release second, version first — so ``152.0.4-beta.28`` is version
    ``152.0.4``, release ``beta.28``. ``Version.fromPath`` reads the two
    keys back out of version.json, and ``fullString`` re-joins them as
    ``<version>-<release>``, so a wrong split shows up as a browser that
    reports the wrong version rather than as a crash.
    """
    version, sep, release = pinned_version.partition("-")
    if not sep:
        raise ValueError(
            f"camoufox pin {pinned_version!r} is not <version>-<release>; "
            "camoufox-js cannot read a version.json without both halves"
        )
    return {"version": version, "release": release}


def _stage_camoufox(pin: PinnedFile, dest: Path, tmp: Path) -> None:
    """The Camoufox browser, plus the version file camoufox-js expects.

    The zip unpacks flat (707 entries, ``camoufox-bin`` at the root), but
    it does NOT contain ``version.json`` — camoufox-js writes that itself
    after its own download, and ``Version.fromPath()`` raises
    ``FileNotFoundError: Version information not found`` without it,
    which its postinstall reports as a broken install. Writing it here is
    what makes a provisioned browser look installed to the library.
    """
    _stage_archive(pin, dest, tmp)
    (dest / "version.json").write_text(
        json.dumps(_camoufox_version_json(pin.version)), encoding="utf-8"
    )


@dataclass(frozen=True)
class _StageContext:
    """What a staging routine may need beyond its own archive.

    Only npm needs either today (it is unpacked BY the node it extends),
    but passing them explicitly is what lets every other routine stay a
    pure archive → directory function with no idea where the store is.
    """

    node: Optional[Path]  # the provisioned node binary, resolved from facts
    npm_cache: Path  # install-scoped npm cache (mutable state, not bytes)


def _stage(
    tool: str,
    pin: PinnedFile,
    dest: Path,
    tmp: Path,
    target: str,
    ctx: _StageContext,
) -> None:
    """Unpack one tool into *dest* — a scratch dir, not its final home.

    Branching lives here and nowhere else: every tool arrives through the
    same fetch-and-verify, and differs only in how its artifact unpacks.
    The caller publishes *dest* into the store with one atomic rename.
    """
    if tool == "git" and target.startswith("win32"):
        _stage_portable_git(pin, dest, tmp)
        return

    if tool == "npm":
        _stage_npm(pin, dest, tmp, ctx, target)
        return

    if tool == "camoufox":
        _stage_camoufox(pin, dest, tmp)
        return

    _stage_archive(pin, dest, tmp)
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
# Floor: 2.31 (rev-parse --path-format=absolute, the newest).
SYSTEM_GIT_FLOOR = (2, 31)


def _macos_xcode_shim(binary: str) -> bool:
    """The /usr/bin/git stub that pops the CLT install dialog is not git."""
    try:
        from installation.env import is_macos_xcode_shim

        return is_macos_xcode_shim(binary)
    except Exception:  # noqa: BLE001 — a probe helper must not take the sweep down
        return False


def probe_system_git() -> Optional[tuple[str, str]]:
    """A usable machine-provided git: ``(absolute_path, version)`` or None.

    Usable means: on PATH, not the xcode-select shim, answers
    ``--version``, and meets ``SYSTEM_GIT_FLOOR`` so every flag this
    codebase passes will be understood. Windows additionally requires
    the managed git regardless (bash.exe ships with PortableGit, and a
    winget/system git's bash may be missing or ASLR-broken), so the
    probe is POSIX-only by design — install.ps1 owns the Windows call.
    """
    if sys.platform == "win32":
        return None
    found = shutil.which("git")
    if found is None or _macos_xcode_shim(found):
        return None
    version = _probe_version(Path(found))
    if version is None:
        return None
    numbers = version.split(".")
    try:
        pair = (int(numbers[0]), int(numbers[1]))
    except (ValueError, IndexError):
        return None
    if pair < SYSTEM_GIT_FLOOR:
        logger.info(
            "system git %s is below the %d.%d floor — provisioning the pinned one",
            version, *SYSTEM_GIT_FLOOR,
        )
        return None
    return found, version


# ─── termux: the verify-only lane (decision 5) ─────────────────────────────
#
# dugite, the node tarballs, gh, ripgrep — every pinned artifact is a
# glibc build, and Termux is bionic. Nothing in the pin table can RUN
# there, so provisioning on Termux does not mean download: it means
# verify that pkg installed what we need and record it, source="system",
# exactly like a floor-clearing system git anywhere else. One lane,
# replacing the is_termux special cases scattered through the callers.
#
# Versions are constraints here, not pins: pkg ships one rolling build
# per tool, so demanding our exact pin would fail every Termux install
# forever. The floor is what the code needs; pkg decides the rest.

TERMUX_TOOLS: dict[str, tuple[tuple[int, int], str]] = {
    # tool: ((major, minor) floor, pkg package name)
    "git": (SYSTEM_GIT_FLOOR, "git"),
    "node": ((20, 0), "nodejs"),  # engine-strict floor for the web UI build
    "npm": ((9, 0), "nodejs"),  # npm rides the nodejs package
    "ripgrep": ((13, 0), "ripgrep"),
    "gh": ((2, 0), "gh"),
    # uv: managed_uv's termux path already owns it (pip lane); not probed.
}


def is_termux() -> bool:
    return bool(
        os.environ.get("TERMUX_VERSION")
        or "com.termux" in os.environ.get("PREFIX", "")
    )


def _provision_termux(
    tool: str,
    facts_dir: Path,
    facts: dict[str, RuntimeFact],
    path_order: list[str] | None,
) -> ToolResult:
    """Verify a pkg-installed tool and record it, or fail with the fix.

    The failure message carries the exact ``pkg install`` line because a
    Termux user cannot be handed a download URL — the store's artifacts
    do not run on bionic, so pkg is the only path and saying anything
    else would be a lie.
    """
    spec = TERMUX_TOOLS.get(tool)
    if spec is None:
        # A tool with no Termux mapping (camoufox, chromium): explicitly
        # unsupported there, not silently skipped.
        return ToolResult(
            tool, "failed", detail=f"{tool} is not available on Termux"
        )
    floor, pkg_name = spec

    fact = facts.get(tool)
    if fact is not None and fact.source == "system" and Path(fact.path).is_file():
        return ToolResult(tool, "kept", version=fact.version)

    found = shutil.which(tool)
    version = _probe_version(Path(found)) if found else None
    pair: tuple[int, int] | None = None
    if version is not None:
        try:
            pair = (int(version.split(".")[0]), int(version.split(".")[1]))
        except (ValueError, IndexError):
            pair = None

    if found is None or version is None or pair is None or pair < floor:
        have = f"{tool} {version}" if version else "nothing"
        return ToolResult(
            tool,
            "failed",
            detail=(
                f"Termux needs {tool} >= {floor[0]}.{floor[1]} from pkg "
                f"(found {have}). Run: pkg install {pkg_name}"
            ),
        )

    facts[tool] = RuntimeFact(version=version, path=found, source="system")
    save_facts(facts, facts_dir, path_order=path_order)
    return ToolResult(tool, "system", version=version)


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
    return {
        "tool": tool,
        "version": pin.version,
        "target": target,
        "sha256": pin.sha256,
        "publishedAt": datetime.now(timezone.utc).isoformat(),
    }


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
        os.replace(staged, entry)
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
) -> ToolResult:
    """Bring ONE tool to the pinned state. Never raises."""
    # Termux first: no pinned artifact runs on bionic, so the pin table
    # governs WHAT tools exist but pkg supplies the bytes (decision 5).
    if is_termux():
        return _provision_termux(tool, facts_dir, facts, path_order)

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
    if tool == "git":
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
                _stage(tool, pin, staging, Path(td), target, ctx)
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
        if _probe_version(binary, env=_probe_env(entry, facts_dir, store)) is None:
            return ToolResult(tool, "failed", detail="provisioned binary does not run")

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
    # prefix this tool actually needs (itself plus what it transitively
    # extends) rather than restating the traversal here.
    needed = extends_closure(tool, pins)
    chain = [name for name in install_order(pins) if name in needed]

    result = ToolResult(tool, "failed", detail=f"{tool} was not provisioned")
    for name in chain:
        result = _provision_one(
            name, pins[name], facts_dir, store, facts, target, path_order=order
        )
        if not result.ok:
            # A dependency that cannot be staged makes the request
            # impossible; report the failure that actually happened.
            return result
    return result


def provision_runtimes(
    runtime_dir: Path | None = None,
    install_root: Path | None = None,
    emit: Callable[[dict], None] | None = None,
    only: list[str] | None = None,
    store_dir: Path | None = None,
) -> list[ToolResult]:
    """Bring every pinned tool to its pinned version.

    Never raises for a single tool — each failure is recorded and the
    rest proceed (a broken ripgrep download must not kill node).

    Tools are provisioned in the pin table's dependency order, so a tool
    that extends another is staged after it — npm is unpacked by running
    the node it extends, which has to exist first.

    Provisioning is always for THIS host. A tool is never recorded until
    the staged binary has answered a version probe here, so a pin that
    downloads but cannot run is a failure rather than a fact.
    """
    facts_dir, store = resolve_bases(runtime_dir, store_dir)
    facts_dir.mkdir(parents=True, exist_ok=True)
    store.mkdir(parents=True, exist_ok=True)
    target = current_target()
    pins = load_pins(install_root)
    facts = load_facts(facts_dir)
    results: list[ToolResult] = []
    order = path_order(pins)

    for tool in install_order(pins):
        if only and tool not in only:
            continue
        # An optional tool is provisioned on demand (provision_tool), not
        # for everyone. Once the facts record it, though, the sweep owns
        # it like any other tool — that is what carries a pin bump onto
        # an install that actually uses the capability.
        if not only and is_optional(tool, pins) and tool not in facts:
            continue
        result = _provision_one(
            tool,
            pins[tool],
            facts_dir,
            store,
            facts,
            target,
            path_order=order,
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
        # third state, and every Termux fact by construction: decision
        # 5's constraints-not-pins rule). Reporting it as drift would
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
        "--only", action="append", help="Provision just this tool (repeatable)."
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

    results = provision_runtimes(
        runtime_dir=ns.runtime_dir, store_dir=ns.store_dir, emit=emit, only=ns.only
    )
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
