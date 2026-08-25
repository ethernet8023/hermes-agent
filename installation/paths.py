"""Where THIS install lives, and where its native tools go.

The bottom of the installation layer: everything else here reads these two
paths, and nothing in this module reads anything of ours. That is what lets
the provisioner import before a venv exists.
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from functools import lru_cache
from pathlib import Path

__all__ = [
    "RUNTIME_DIR_NAME",
    "TOOL_STORE_DIR_NAME",
    "get_install_root",
    "get_runtime_dir",
    "get_tool_store",
    "reset_install_root_override",
    "resolve_bases",
    "set_install_root_override",
]

RUNTIME_DIR_NAME = ".hermes-runtime"
TOOL_STORE_DIR_NAME = "tools"

# A module-private sentinel: ``None`` is a meaningful override value here
# (it means "restore the default derivation"), so absence needs its own.
_UNSET = object()

_INSTALL_ROOT_OVERRIDE: ContextVar[str | object] = ContextVar(
    "_INSTALL_ROOT_OVERRIDE", default=_UNSET
)


def set_install_root_override(path: str | Path | None) -> Token:
    """Override the install root for this context (desktop resourcesPath,
    tests). Pass ``None`` to explicitly restore the default derivation."""
    value: str | object = _UNSET if path is None else str(path)
    return _INSTALL_ROOT_OVERRIDE.set(value)


def reset_install_root_override(token: Token) -> None:
    _INSTALL_ROOT_OVERRIDE.reset(token)


@lru_cache(maxsize=8)
def _stamped_runtime_dir(install_root: str) -> str | None:
    """The install stamp's ``runtimeDir``, or None.

    THE one stamp read in this module, cached per root: a sealed payload's
    stamp names where its runtime dir sits (RELATIVE to the stamp, ``..``
    for the desktop payload, whose runtime dir is the payload dir itself).
    Reading it here — instead of every launcher exporting
    ``HERMES_RUNTIME_DIR`` — means the CLI shim, the Electron spawn, and a
    bare ``python -m`` all resolve the same layout from the artifact alone.

    Absolute values are refused as staging bugs (same rule as the shim's
    sidecar): the whole point of the field is relocatability.
    """
    import json

    try:
        data = json.loads(
            (Path(install_root) / "install-stamp.json").read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError):
        return None
    rel = data.get("runtimeDir") if isinstance(data, dict) else None
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
        return None
    return rel


def get_install_root() -> Path:
    """Return the root directory of THIS install of Hermes.

    Resolution order:
      1. ``HERMES_INSTALL_ROOT`` env var — set by the desktop app
         (resources payload) and by tests. An env var rather than only a
         ContextVar because child processes (post-update phase, tool
         subprocesses) must inherit it across the process boundary.
      2. Context override (``set_install_root_override``) — in-process
         callers that cannot mutate the environment.
      3. The directory ABOVE this package — for a source checkout that is
         the repo root, since ``installation/`` sits at top level.

    pip/wheel layouts are unsupported by design (setup.py blocks wheel
    builds outside Nix), so rung 3 is always a real, writable checkout —
    or the caller set rung 1/2.
    """
    env_root = os.environ.get("HERMES_INSTALL_ROOT", "")
    if env_root:
        return Path(env_root)
    override = _INSTALL_ROOT_OVERRIDE.get()
    if override is not _UNSET:
        return Path(str(override))
    return Path(__file__).resolve().parent.parent


def get_runtime_dir(install_root: Path | None = None) -> Path:
    """Return the runtime directory holding ``runtimes.json`` and tool state.

    Holds managed binaries (node, npm, uv, git, gh, ripgrep), install-keyed
    caches, and the ``runtimes.json`` facts manifest. Callers must treat
    the location as opaque and go through the runtime registry for tool
    lookup — no path literals.

    Resolution:
      1. ``HERMES_RUNTIME_DIR`` — packagers that BUILD the runtime dir
         somewhere unrelated to the tree (Nix: an immutable store path no
         provisioner can write to).
      2. The install stamp's ``runtimeDir`` (relative to the stamp) — a
         sealed payload whose runtime dir is part of the artifact but not
         at the default spot (the desktop payload: ``..``, the payload dir
         itself). Derived from the artifact, so every launcher — GUI spawn,
         CLI shim, bare ``python -m`` — resolves it identically with no
         env contract.
      3. ``<install root>/.hermes-runtime`` — source checkouts.

    An explicit *install_root* skips rung 1 — a caller naming a root means
    that root, not the process-wide override.
    """
    if install_root is None:
        override = os.environ.get("HERMES_RUNTIME_DIR", "").strip()
        if override:
            return Path(override)
    root = install_root if install_root is not None else get_install_root()
    stamped = _stamped_runtime_dir(str(root))
    if stamped is not None:
        return (root / stamped).resolve()
    return root / RUNTIME_DIR_NAME


def get_tool_store() -> Path:
    """Return the machine-wide store that holds managed tool BYTES.

    ``~/.hermes/tools/<tool>-<version>-<target>/`` — one entry per
    (tool, version, target) tuple, which is exactly the tuple the pin
    table keys on. Two installs that agree on a pin therefore share the
    entry by construction, and two that disagree get one entry each.

    Bytes and FACTS live apart on purpose. The facts file stays
    install-scoped in ``get_runtime_dir()`` because which tools an
    install uses is its own business; the bytes are identical wherever
    they came from, so copying them per install only costs disk. A
    checkout-nested copy cost ~495MB per worktree, and worktrees are the
    normal unit of work here.

    There are no symlinks between the two: ``runtimes.json`` names a
    store-relative path, so the facts file IS the indirection layer.

    ``HERMES_RUNTIME_DIR`` wins, and points bytes and facts back at ONE
    self-contained directory. That is what a packager builds: the Nix
    bundle assembles a runtime dir at build time and cannot use a store
    it does not own. The install stamp's ``runtimeDir`` says the same
    thing from inside the artifact — the desktop payload is its own
    store — so it collapses the pair identically.
    """
    override = os.environ.get("HERMES_RUNTIME_DIR", "").strip()
    if override:
        return Path(override)
    root = get_install_root()
    stamped = _stamped_runtime_dir(str(root))
    if stamped is not None:
        return (root / stamped).resolve()
    # Local import: hermes_constants imports THIS module, so an
    # import-time dependency would be circular. By the time anyone calls
    # this, both modules are loaded. (installation/tree.py does the same
    # for the same reason.)
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / TOOL_STORE_DIR_NAME


def resolve_bases(
    runtime_dir: Path | None = None, store_dir: Path | None = None
) -> tuple[Path, Path]:
    """Resolve the (facts dir, bytes dir) pair every reader needs.

    ONE rule, shared by the registry, the environment assembler and the
    provisioner, so no two of them can disagree about where a tool is:

    * an explicit *store_dir* always wins;
    * a caller that names a *runtime_dir* and no store means THAT
      directory for both — a self-contained runtime dir, which is what
      the Nix bundle, the desktop payload and the tests all pass;
    * with neither, facts come from this install's runtime dir and bytes
      from the shared store.
    """
    rt = runtime_dir if runtime_dir is not None else get_runtime_dir()
    if store_dir is not None:
        return rt, store_dir
    if runtime_dir is not None:
        return rt, rt
    return rt, get_tool_store()
