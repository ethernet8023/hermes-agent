"""The ONE place that turns runtime-registry facts into process environment.

Every Hermes-spawned subprocess that should see managed tools gets its
PATH (and tool-specific env) from here — locators, gateway spawns, the
desktop backend (mirrored in apps/desktop/electron/backend-env.ts; a
cross-language test keeps the two in lockstep). Managed tools go at the
FRONT of PATH so they override system ones uniformly.

Also owns per-tool environment: npm's package cache is pointed into the
install's runtime cache dir so `~/.npm` stops accumulating install-coupled
state.

Design doc: .hermes/plans/2026-08-12_hermes-home-lifetime-split.md (phase 1).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Optional

from installation.paths import get_runtime_dir, resolve_bases
from installation.registry import (
    PLAYWRIGHT_BROWSER_TOOLS,
    RuntimeFact,
    load_facts,
    load_path_order,
)

__all__ = [
    "managed_path_dirs",
    "managed_tool_env",
    "runtime_cache_dir",
    "with_managed_runtimes",
]


def runtime_cache_dir(runtime_dir: Path | None = None) -> Path:
    """Install-keyed cache root: <runtime dir>/cache."""
    base = runtime_dir if runtime_dir is not None else get_runtime_dir()
    return base / "cache"


def _dirs_for(fact: RuntimeFact, base: Path) -> list[Path]:
    if fact.path_dirs is not None:
        return [base / d for d in fact.path_dirs]
    return [(base / fact.path).parent]


def managed_path_dirs(
    runtime_dir: Path | None = None, store_dir: Path | None = None
) -> list[Path]:
    """Existing bin dirs of every provisioned tool, in assembly order.

    The order is DATA, recorded in the facts file by the provisioner from
    the pin table's ``extends`` edges — a tool that extends another must
    be found first, or the copy it supersedes wins (npm before node, or
    node's bundled npm shadows the pinned one). Nothing here restates it.

    Tools absent from facts (or recorded but vanished) contribute nothing:
    an unprovisioned install degrades to system tools instead of shipping
    dead PATH entries.

    Facts come from the install, bytes from the store — the fact's path is
    store-relative, so the dirs this emits point INTO the shared store.
    """
    facts_dir, store = resolve_bases(runtime_dir, store_dir)
    facts = load_facts(facts_dir)
    dirs: list[Path] = []
    for tool in load_path_order(facts_dir):
        fact = facts.get(tool)
        if fact is None or not (store / fact.path).is_file():
            continue
        # A system-provided tool is already on the machine's own PATH.
        # Promoting its directory into the MANAGED prefix would hoist
        # everything else living there (/usr/bin) above the managed
        # tools, which is the exact shadowing this order exists to stop.
        if fact.source == "system":
            continue
        for d in _dirs_for(fact, store):
            if d.is_dir() and d not in dirs:
                dirs.append(d)
    return dirs


# macOS paths that are the xcode-select STUB, not a real tool. On a Mac
# without the Command Line Tools these do nothing but pop a modal
# "install developer tools?" dialog — which a background agent process
# must never trigger.
_MACOS_XCODE_SHIMS = frozenset({"/usr/bin/git", "/usr/bin/xcrun"})


def is_macos_xcode_shim(binary: str | Path | None) -> bool:
    """True when *binary* is the macOS developer-tools stub."""
    if not binary or sys.platform != "darwin":
        return False
    return str(binary) in _MACOS_XCODE_SHIMS


def managed_tool_binary(
    tool: str,
    runtime_dir: Path | None = None,
    store_dir: Path | None = None,
) -> Optional[Path]:
    """The managed binary for *tool*, or None when it is not provisioned.

    The single lookup every locator should use before falling back to a
    system copy — resolves from the registry facts, so it knows about a
    tool the moment the provisioner records it.
    """
    facts_dir, store = resolve_bases(runtime_dir, store_dir)
    fact = load_facts(facts_dir).get(tool)
    if fact is None:
        return None
    binary = store / fact.path
    return binary if binary.is_file() else None


def managed_tool_env(
    runtime_dir: Path | None = None, store_dir: Path | None = None
) -> dict[str, str]:
    """Tool-specific env for managed runtimes.

    - npm_config_cache: npm's package cache → install-keyed cache dir,
      only when node is managed (a system node keeps the user's ~/.npm).
    - GIT_EXEC_PATH / GIT_TEMPLATE_DIR / GIT_CONFIG_SYSTEM / GIT_SSL_CAINFO
      / PREFIX: the portable-git contract, and the same set dugite's own
      ``setupEnvironment()`` exports before every invocation. A
      relocatable git resolves helpers, templates and config against its
      BUILD-time prefix, so a copy running from anywhere else needs to be
      told where it actually lives; dugite's source says so directly
      ("when building Git for Linux and then running it from an
      arbitrary location, you should set PREFIX"). Pointing at them
      explicitly also makes the managed git immune to a child process
      rewriting PATH, and stops it reading /etc/gitconfig from whatever
      machine it landed on. Only emitted for a layout that actually has
      these dirs (dugite-native / PortableGit both do); a system git is
      left entirely alone, because exporting GIT_EXEC_PATH at a git we do
      not own breaks it.
    """
    facts_dir, store = resolve_bases(runtime_dir, store_dir)
    facts = load_facts(facts_dir)
    env: dict[str, str] = {}
    node = facts.get("node")
    if node is not None and (store / node.path).is_file():
        # The cache is install-scoped even though the bytes are shared:
        # it is mutable state this install writes, not an artifact.
        env["npm_config_cache"] = str(runtime_cache_dir(facts_dir) / "npm")

    git = facts.get("git")
    if git is not None and git.source != "system" and (store / git.path).is_file():
        root = (store / git.path).parent.parent
        for key, relative in (
            ("GIT_EXEC_PATH", Path("libexec") / "git-core"),
            ("GIT_TEMPLATE_DIR", Path("share") / "git-core" / "templates"),
            ("GIT_CONFIG_SYSTEM", Path("etc") / "gitconfig"),
            ("GIT_SSL_CAINFO", Path("ssl") / "cacert.pem"),
        ):
            target = root / relative
            if target.exists():
                env[key] = str(target)
        # PREFIX is the git root itself, not a file under it, so it has
        # no existence probe of its own — the git fact already proved the
        # tree is there. Linux only: dugite sets it only on linux, and it
        # is a generic-enough name that exporting it on other platforms
        # risks colliding with unrelated build tooling.
        if sys.platform.startswith("linux"):
            env["PREFIX"] = str(root)

    # The playwright browsers are resolved by playwright itself, by
    # DIRECTORY NAME under one root — their store entries are named
    # playwright's way for exactly this (see store_entry_name), so the
    # browsers path is simply the store. Only exported when a browser
    # fact exists: pointing playwright at the store on installs that
    # never browsed would stop `npx playwright install` runs the user
    # does for their own projects from landing in the default cache.
    for browser in PLAYWRIGHT_BROWSER_TOOLS:
        fact = facts.get(browser)
        if fact is not None and (store / fact.path).is_file():
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(store)
            break
    return env


def with_managed_runtimes(
    env: Optional[Mapping[str, str]] = None,
    runtime_dir: Path | None = None,
    store_dir: Path | None = None,
) -> dict[str, str]:
    """Return a copy of *env* (default: os.environ) with managed tool dirs
    prepended to PATH and tool env applied. The single entry point —
    callers never assemble PATH fragments themselves."""
    result = dict(os.environ if env is None else env)
    dirs = managed_path_dirs(runtime_dir, store_dir)
    if dirs:
        path_key = next((k for k in result if k.upper() == "PATH"), "PATH")
        existing = result.get(path_key, "")
        prefix = os.pathsep.join(str(d) for d in dirs)
        result[path_key] = f"{prefix}{os.pathsep}{existing}" if existing else prefix
    # Tool env never clobbers explicit caller settings.
    for key, value in managed_tool_env(runtime_dir, store_dir).items():
        result.setdefault(key, value)
    return result
