"""Run the pinned Node and npm.

Every Hermes install provisions the pin table's tools before any code runs —
the installers treat a failed provision as a failed install, Nix and Docker
build the runtime dir into the artifact, and the dev shell points at the same
built dir. So Node and npm are always present, always the pinned versions, and
already proven to run: the provisioner records a tool as a fact only after
executing it.

That guarantee is what this module is for. Callers ask for "run npm here", not
"find me an npm, check it works, check its version, heal it if it does not,
fall back to whatever is on PATH". All of that machinery existed because the
toolchain was a maybe; it is not one any more.

``engines`` in the root ``package.json`` is satisfied by the pins as a matter
of arithmetic (node >=22.22.0 against a pinned 26.7.0, npm >=11.17.0 against a
pinned 12.0.2), so ``EBADENGINE`` cannot happen through these functions and
nothing here tries to recover from it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from installation import env as runtime_env
from installation import registry

__all__ = [
    "NotProvisioned",
    "lockfile_changed",
    "manifests_digest",
    "node_path",
    "npm_install",
    "npm_path",
    "npx_path",
    "record_lockfile_hash",
    "run_node",
    "run_npm",
    "run_npx",
]

class NotProvisioned(RuntimeError):
    """A pinned tool is missing from an install that must have provisioned it.

    Not a fallback signal. Every install shape provisions before running, so
    this means the runtime dir was damaged after the fact — recoverable only
    by re-provisioning, not by reaching for a system copy of unknown version.
    """


def _binary(tool: str) -> Path:
    resolved = registry.tool_path(tool)
    if resolved is None:
        raise NotProvisioned(
            f"{tool} is not in this install's runtime dir "
            f"({registry.facts_path().parent}). Every Hermes install "
            f"provisions the pinned tools, so this tree is damaged: run "
            f"`hermes update` to re-provision it."
        )
    return resolved


def node_path() -> Path:
    """The pinned node binary."""
    return _binary("node")


def npm_path() -> Path:
    """The pinned npm binary."""
    return _binary("npm")


def npx_path() -> Path:
    """The pinned npx.

    npx is not pinned separately — it ships inside the npm tree, beside the
    npm binary the pin table names — so it is located relative to npm rather
    than given a fact of its own.
    """
    candidate = npm_path().parent / ("npx.cmd" if os.name == "nt" else "npx")
    if not candidate.exists():
        raise NotProvisioned(
            f"npx is missing from the managed npm tree at {candidate.parent}. "
            "Run `hermes update` to re-provision it."
        )
    return candidate


def _run(
    argv: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    capture_output: bool = True,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run *argv* with the managed toolchain on PATH.

    PATH and the tool env travel together because some pinned tools need
    both: npm's shim is ``#!/usr/bin/env node`` and resolves its interpreter
    from PATH, and a relocated git finds its helpers through ``GIT_EXEC_PATH``.
    Passing one without the other is how a tool that is present still fails to
    run.
    """
    merged = runtime_env.with_managed_runtimes(env)
    return subprocess.run(
        [str(a) for a in argv],
        cwd=str(cwd) if cwd is not None else None,
        env=merged,
        capture_output=capture_output,
        text=True,
        check=check,
        timeout=timeout,
    )


def run_node(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    capture_output: bool = True,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run the pinned node with *args*."""
    return _run(
        [node_path(), *args],
        cwd=cwd,
        env=env,
        capture_output=capture_output,
        check=check,
        timeout=timeout,
    )


def run_npm(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    capture_output: bool = True,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run the pinned npm with *args*."""
    return _run(
        [npm_path(), *args],
        cwd=cwd,
        env=env,
        capture_output=capture_output,
        check=check,
        timeout=timeout,
    )


def run_npx(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    capture_output: bool = True,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run the pinned npx with *args*."""
    return _run(
        [npx_path(), *args],
        cwd=cwd,
        env=env,
        capture_output=capture_output,
        check=check,
        timeout=timeout,
    )


def npm_install(
    project_dir: Path,
    *,
    extra_args: Sequence[str] = (),
    capture_output: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Install *project_dir*'s dependencies without mutating its lockfile.

    ``npm ci`` when a lockfile is present, because it is strict and leaves the
    lockfile alone. It falls back to ``npm install --no-save`` only when
    ``npm ci`` fails, which on a WIP branch means the lockfile is out of sync
    with package.json. ``--no-save`` keeps the fallback honest: without it an
    out-of-sync lockfile gets rewritten, every later ``npm ci`` fails against
    the drifted file, and the tree reinstalls forever (PR #65595).

    ``--include=dev`` on both paths. The callers are frontend builds that need
    ``tsc`` / ``vite`` / ``electron-builder``, all devDependencies, and npm
    silently omits those — exit 0, no error — when ``NODE_ENV=production`` or
    ``omit=dev`` leaks in from a shell profile, a container image, or the
    bundled TUI launcher. The flag overrides both, which scrubbing the
    environment would not.
    """
    # unicode-animations' postinstall animates to /dev/tty, bypassing
    # --silent and capture_output. It no-ops when CI is set.
    run_env = {**os.environ, **(env or {}), "CI": "1"}
    # esbuild treats this as an executable override. If a shell points it
    # at a different release, the pinned package's postinstall rejects
    # that binary and the whole install aborts (#87405). Every caller is
    # a frontend build against the pinned toolchain, so the override is
    # never right here.
    run_env.pop("ESBUILD_BINARY_PATH", None)
    npm = npm_path()

    if (project_dir / "package-lock.json").exists():
        result = _run(
            [npm, "ci", "--include=dev", *extra_args],
            cwd=project_dir,
            env=run_env,
            capture_output=capture_output,
        )
        if result.returncode == 0:
            return result

    return _run(
        [npm, "install", "--no-save", "--include=dev", *extra_args],
        cwd=project_dir,
        env=run_env,
        capture_output=capture_output,
    )


def _manifest_paths(project_dir: Path) -> tuple[Path, ...]:
    """The manifests whose contents decide whether an install is current.

    The lockfile alone is not a sufficient key: a developer can edit any
    package.json without running npm, leaving the lockfile untouched while
    the tree needs syncing.

    Workspace members come from the root package.json's own ``workspaces``
    globs — npm's source of truth — so adding a workspace cannot silently
    escape the key. Every member counts, even ones a given install command
    does not name, because one lockfile spans the whole graph and any
    manifest edit can put it out of sync. An unreadable root manifest falls
    back to the root pair, which over-installs rather than wrongly skipping.
    """
    lockfile = project_dir / "package-lock.json"
    root_manifest = project_dir / "package.json"
    paths = [lockfile, root_manifest]
    try:
        workspaces = json.loads(root_manifest.read_text(encoding="utf-8")).get(
            "workspaces", []
        )
    except (OSError, ValueError):
        return tuple(paths)
    if isinstance(workspaces, dict):  # legacy {"packages": [...]} form
        workspaces = workspaces.get("packages", [])
    for pattern in workspaces:
        for match in sorted(project_dir.glob(str(pattern))):
            manifest = match / "package.json"
            if manifest.is_file():
                paths.append(manifest)
    return tuple(paths)


def manifests_digest(project_dir: Path) -> str | None:
    """Digest of *project_dir*'s dependency manifests, or None without a lockfile.

    None means "no lockfile to compare against", which callers must treat as
    "assume changed" rather than "unchanged" — a missing lockfile is not
    evidence that an install is current.

    Each path is hashed along with its name so that moving a manifest changes
    the digest even when the bytes are identical.
    """
    if not (project_dir / "package-lock.json").exists():
        return None
    digest = hashlib.sha256()
    for path in _manifest_paths(project_dir):
        digest.update(str(path.relative_to(project_dir)).encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()


def _hash_cache_path(state_dir: Path, project_dir: Path) -> Path:
    # Keyed by project dir so parallel worktrees do not read each other's
    # answer and skip an install they actually need.
    key = hashlib.sha256(str(project_dir).encode()).hexdigest()[:12]
    return state_dir / f".npm_lock_hash_{key}"


def lockfile_changed(state_dir: Path, project_dir: Path) -> bool:
    """True when *project_dir* needs an install.

    Errs toward reinstalling. A missing digest, a missing cache file, an
    unreadable cache, or a missing ``node_modules`` all count as changed: the
    cost of a redundant install is a slow update, and the cost of a wrongly
    skipped one is a broken tree that stays broken on every subsequent run.
    """
    current = manifests_digest(project_dir)
    if current is None:
        return True
    if not (project_dir / "node_modules").is_dir():
        return True
    cache_file = _hash_cache_path(state_dir, project_dir)
    try:
        return cache_file.read_text(encoding="utf-8").strip() != current
    except OSError:
        return True


def record_lockfile_hash(state_dir: Path, project_dir: Path) -> None:
    """Record *project_dir*'s manifest digest after a successful install."""
    digest = manifests_digest(project_dir)
    if digest is None:
        return
    try:
        _hash_cache_path(state_dir, project_dir).write_text(digest, encoding="utf-8")
    except OSError:
        # A cache we cannot write means the next run reinstalls. Slow, correct.
        pass
