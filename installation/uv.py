"""Locate and ensure the pinned uv.

The uv story is the same as node's (see ``installation/nodejs.py``): every
install shape provisions the pin table's tools, so uv is normally present,
pinned, and already proven to run — the provisioner records a fact only
after executing the staged binary. Callers ask for the path (``uv_path``)
or ask for it to exist (``ensure_uv``); nobody re-implements resolution.

This replaces the acquisition half of the retired ``hermes_cli.managed_uv``,
which predated the store and hardcoded its own layout (``<runtime>/uv/uv``)
— a second spelling of what the facts file already records. The venv/SQLite
repair half of that module lives on as ``hermes_cli.runtime_repair``; it is
checkout venv surgery, not tool acquisition, and callers that want it call
it explicitly.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from installation import registry

logger = logging.getLogger(__name__)

__all__ = ["ensure_uv", "uv_path", "uvx_path"]


def uv_path() -> Optional[Path]:
    """The pinned uv binary, or None when not provisioned.

    Pure lookup through the runtime registry — facts from the runtime dir
    (HERMES_RUNTIME_DIR override, the install stamp's ``runtimeDir``, or
    ``<install root>/.hermes-runtime``), bytes from the store the facts
    name. A recorded-but-vanished binary reads as unprovisioned.

    Every install shape provisions the pin table's tools before running,
    so ``None`` here is a damaged or half-built install, not a state to
    quietly route around — callers should surface it (the provisioner
    command in an error message), never fall back to a PATH uv of
    unknown version.
    """
    resolved = registry.tool_path("uv")
    if resolved is not None and os.access(resolved, os.X_OK):
        return resolved
    return None


def uvx_path() -> Optional[Path]:
    """The pinned uvx binary, or None when uv is not provisioned.

    uvx ships inside uv's own artifact, beside the uv binary in the same
    store entry — the pin that governs uv governs it. Resolved from the
    uv fact rather than by probing directories: the registry names the
    binary, so nobody goes fishing with ``shutil.which`` on a dir.
    """
    uv = uv_path()
    if uv is None:
        return None
    candidate = uv.parent / ("uvx.exe" if uv.name.endswith(".exe") else "uvx")
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    return None


def ensure_uv() -> Optional[str]:
    """The pinned uv path, converging on the pin table first.

    Always runs the provisioner: its ``kept`` fast path is a facts-file
    equality check against the pin (no network when current), a missing
    tool is downloaded, and a pin that moved since the last provision is
    re-staged — so "ensure" and "converge after git pull" are one call.
    Downloads verify the pinned sha256 BEFORE extraction, and the fact is
    recorded only after the staged binary answers a version probe.

    Returns the path as ``str`` (subprocess-argv-safe on every platform)
    or ``None`` when uv cannot be provisioned — never raises, so callers
    can degrade with a clear message. Hot paths that must not touch the
    provisioner use ``uv_path()`` instead.
    """
    from installation.provisioner import provision_tool

    try:
        result = provision_tool("uv")
        if not result.provisioned:
            logger.warning("pinned uv provisioning failed: %s", result.detail)
    except Exception as exc:  # noqa: BLE001 — acquisition degrades, never raises
        logger.warning("pinned uv provisioning failed: %s", exc)
    # Resolve through the registry either way: a failed convergence with a
    # working (older) staged uv should return that binary, not None.
    resolved = uv_path()
    return str(resolved) if resolved is not None else None
