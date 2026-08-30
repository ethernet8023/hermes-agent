"""Which llama.cpp build this machine runs, and where its state lives.

The engine BINARIES are pm packages (``llamacpp-cuda`` and friends): pm owns
the urls, digests, download, extraction, and the machine-wide store, exactly
as it does for node, ripgrep, and the browser. This module answers only the
questions the build cannot:

- which backend this machine's hardware can use, and
- where the runtime's own STATE goes (presets, server endpoint, api key,
  window overrides) — state is not bytes and does not belong in the store.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Backend -> pm package. Backends are dlopen'd plugins built into separate
# release archives, so each is its own optional package; upstream's gaps
# (no prebuilt Linux CUDA, no win-arm64 vulkan) are declared there and
# surface as an honest refusal instead of a 404 mid-download.
BACKEND_PACKAGES = {
    "cuda": "llamacpp-cuda",
    "vulkan": "llamacpp-vulkan",
    "metal": "llamacpp-metal",
    "cpu": "llamacpp-cpu",
}

# Preference order when the configured backend is unavailable here.
_LADDER = ("cuda", "metal", "vulkan", "cpu")


class BinaryResolutionError(RuntimeError):
    """No usable llama.cpp build for this platform/backend."""


def runtime_state_root() -> Path:
    """Machine-scoped, deliberately NOT profile-scoped. Presets, the server
    endpoint, and the api key describe this machine's hardware and its one
    managed server (stable port) — a second profile fighting over the port
    would be the bug. Profile-scoped things (which model is the default,
    enabled) live in each profile's config.yaml as ever.

    Only STATE lives here; the binaries are in pm's store, shared the same
    way and garbage-collected by `hermes pm gc`.
    """
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "runtimes" / "llamacpp"


def models_root() -> Path:
    """Machine-scoped for the same reason: a 20 GB GGUF is a machine asset,
    and every profile shares the one managed server that serves it."""
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "models"


def select_backend(gpu_vendor: str | None, os_name: str | None = None) -> str:
    """Backend choice per design: CUDA if NVIDIA, Metal on macOS, Vulkan if
    a non-NVIDIA GPU is present, else CPU. ``--list-devices`` validates the
    choice post-install; the supervisor's touch generation is ground truth."""
    import sys

    if os_name is None:
        os_name = "macos" if sys.platform == "darwin" else "other"
    if os_name == "macos":
        return "metal"
    vendor = (gpu_vendor or "").lower()
    if "nvidia" in vendor:
        return "cuda"
    if vendor in ("amd", "intel") or "radeon" in vendor or "arc" in vendor:
        return "vulkan"
    return "cpu"


def unavailable_reason(backend: str) -> str | None:
    """Why this backend cannot run here, or None when it can. The reason is
    the pin table's own declared gap, quoted verbatim — an unpinned
    combination is knowable before any download starts."""
    from pm.registry import get_package
    from pm.store import current_target

    name = BACKEND_PACKAGES.get(backend)
    if name is None:
        return f"unknown backend {backend}"
    return get_package(name).missing_reason(current_target())


def resolve_backend(backend: str) -> str:
    """The backend to actually install: the requested one when this
    platform has a build, else the best available rung below it. Raises
    when nothing is available (which the pin table makes impossible in
    practice — every target has a cpu build)."""
    tried = [backend] + [b for b in _LADDER if b != backend]
    reasons = []
    for candidate in tried:
        reason = unavailable_reason(candidate)
        if reason is None:
            if candidate != backend:
                logger.info("backend %s unavailable here; using %s", backend, candidate)
            return candidate
        reasons.append(f"{candidate}: {reason}")
    raise BinaryResolutionError("; ".join(reasons))


def installed_backends() -> list[str]:
    """Backends with an engine on disk, in ladder order. Deliberately NOT
    version-gated: an engine from a previous pin still serves, and the
    boot ladder must keep working after a lockfile bump. One resolver —
    the boot ladder and the status pane both read from here."""
    from pm.ensure import _facts, _store

    facts = _facts()
    root = _store().root
    return [
        b for b in _LADDER if facts.installed(BACKEND_PACKAGES[b], None, root)
    ]


def engine_update_pending() -> bool:
    """True when an installed engine is older than the pinned one. The
    lockfile is the version authority, so an update arrives with a Hermes
    release; installing it is a button click, never automatic."""
    from pm.ensure import is_installed

    installed = installed_backends()
    return bool(installed) and not all(
        is_installed(BACKEND_PACKAGES[b]) for b in installed
    )


def engine_version() -> str | None:
    """The pinned llama.cpp release tag (bNNNN). The lockfile is the one
    version authority; there is no user-settable tag to disagree with it."""
    from pm.ensure import _lockfile

    version = _lockfile().version("llamacpp-cpu")
    return f"b{version}" if version else None


def installed_engine_version(backend: str) -> str | None:
    """The release tag of an installed backend's engine, or None."""
    from pm.ensure import _facts

    fact = _facts().get(BACKEND_PACKAGES.get(backend, ""))
    return f"b{fact['version']}" if fact else None


def server_binary(backend: str) -> Path:
    """llama-server for an INSTALLED backend. Never installs — callers that
    want an install call ensure_engine()."""
    from pm.ensure import _facts, _store
    from pm.registry import get_package
    from pm.store import current_target

    name = BACKEND_PACKAGES.get(backend)
    if name is None:
        raise BinaryResolutionError(f"unknown backend {backend}")
    fact = _facts().get(name)
    if fact is None:
        raise BinaryResolutionError(f"llama.cpp {backend} build is not installed")
    binary = get_package(name).binary(_store().entry(fact["entry"]), current_target())
    if binary is None or not binary.is_file():
        raise BinaryResolutionError(f"llama.cpp {backend} entry is missing its binary")
    return binary


def ensure_engine(backend: str, progress=None) -> tuple[str, Path]:
    """Install (if needed) and return ``(backend, llama-server path)``.

    Idempotent, digest-verified, and shared machine-wide — all of that is
    pm's. ``progress(stage, done, total, label)`` ticks through the slow
    parts for the install pane.

    Marked explicit: every caller is a deliberate "install the engine"
    click, which is exactly the consent the lazy-install policy asks for.
    A sealed payload still refuses — asking a bundle for more than it
    shipped is a packaging bug, not something a download can fix. Nothing
    on the boot path calls this; boot serves what is already installed.
    """
    from pm.ensure import ensure
    from pm.package import InstallError

    resolved = resolve_backend(backend)
    try:
        ensure(BACKEND_PACKAGES[resolved], progress=progress, explicit=True)
    except InstallError as exc:
        raise BinaryResolutionError(str(exc)) from exc
    return resolved, server_binary(resolved)
