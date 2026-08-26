"""The managed llama.cpp engine: where it lives, and which backend to use.

The engine is a PINNED TOOL like every other managed binary — the pin
table (``installation/runtime-pins.json``) carries its exact URL and
sha256 per target, and the provisioner downloads, verifies, extracts and
version-probes it into the machine-wide tool store. There is no
download, no digest check and no extraction here; that machinery exists
once, in ``installation/``, and this module only names the tool and
locates what was installed.

Backend selection stays here because it is a RUNTIME question — it reads
the GPU present on this machine, which a build-time pin cannot know. The
pin table names one tool per backend (``llamacpp-cuda``, and its
siblings as they are pinned); this module decides which of those names
to ask for.

``$HERMES_HOME/runtimes/llamacpp`` remains the engine's STATE directory
(presets, server.json, the api key) — machine-scoped, deliberately not
profile-scoped, because those describe this machine's hardware and its
one managed server.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Pin-table tool name per backend. A backend absent from this map has no
# pinned engine yet: resolution refuses rather than guessing a URL.
_TOOL_BY_BACKEND = {
    "cuda": "llamacpp-cuda",
}


class BinaryResolutionError(RuntimeError):
    """No usable engine for this platform/backend."""


def runtimes_root() -> Path:
    """The engine's STATE directory (presets, server state, api key).

    Machine-scoped, deliberately NOT profile-scoped: presets and server
    state describe this machine's hardware and its one managed server
    (stable port), so a second profile fighting over the port would be
    the bug. Profile-scoped things (which model is the default, enabled)
    live in each profile's config.yaml as ever.

    The engine BINARIES are not here — they are a pinned tool in the
    machine-wide tool store, resolved through ``engine_dir()``.
    """
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "runtimes" / "llamacpp"


def backend_tool(backend: str) -> str:
    """The pin-table tool name serving *backend*."""
    tool = _TOOL_BY_BACKEND.get(backend)
    if tool is None:
        raise BinaryResolutionError(
            f"no pinned llama.cpp engine for backend {backend!r} "
            f"(pinned: {', '.join(sorted(_TOOL_BY_BACKEND)) or 'none'})"
        )
    return tool


def _host_os_arch() -> tuple[str, str]:
    """(os, arch) for backend selection.

    PITFALL: PROCESSOR_ARCHITECTURE lies under x64 emulation on ARM64
    Windows. platform.machine() reads the same env on some Pythons, so on
    Windows prefer PROCESSOR_IDENTIFIER's text when present.
    """
    system = platform.system().lower()
    os_name = {"windows": "win", "darwin": "macos", "linux": "ubuntu"}.get(system, system)
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if os_name == "win":
        import os as _os

        ident = _os.environ.get("PROCESSOR_IDENTIFIER", "")
        if "armv8" in ident.lower() or "arm " in ident.lower():
            arch = "arm64"
    return os_name, arch


def select_backend(gpu_vendor: str | None, os_name: str | None = None) -> str:
    """Backend choice per design: CUDA if NVIDIA, Metal on macOS, Vulkan if
    a non-NVIDIA GPU is present, else CPU. ``--list-devices`` validates the
    choice post-install; the supervisor's touch generation is ground truth."""
    if os_name is None:
        os_name, _ = _host_os_arch()
    if os_name == "macos":
        return "metal"
    vendor = (gpu_vendor or "").lower()
    if "nvidia" in vendor:
        return "cuda"
    if vendor in ("amd", "intel") or "radeon" in vendor or "arc" in vendor:
        return "vulkan"
    return "cpu"


def engine_dir(backend: str) -> Path | None:
    """The installed engine's directory for *backend*, or None.

    None means "not provisioned here" — the boot ladder serves what is
    installed and never downloads, so a missing engine is a state to
    report, not an error to raise.
    """
    from installation.registry import tool_path

    exe = tool_path(backend_tool(backend))
    return exe.parent if exe is not None else None


def server_binary(install_dir: Path) -> Path:
    """Locate llama-server within an engine directory."""
    for name in ("llama-server.exe", "llama-server"):
        candidate = install_dir / name
        if candidate.is_file():
            return candidate
    raise BinaryResolutionError(f"llama-server not found under {install_dir}")


def installed_backends() -> list[str]:
    """Backends with a provisioned engine on this machine.

    Reads the provisioner's facts — the same authority every other
    managed tool is looked up through — so "installed" means the same
    thing here as it does for node or uv.
    """
    from installation.registry import tool_path

    found = []
    for backend, tool in _TOOL_BY_BACKEND.items():
        if tool_path(tool) is not None:
            found.append(backend)
    return found


def installed_version(backend: str) -> str | None:
    """The provisioned engine's build number for *backend*, or None."""
    from installation.registry import load_facts

    fact = load_facts().get(backend_tool(backend))
    return fact.version if fact is not None else None


def pinned_version(backend: str) -> str | None:
    """The build number the pin table names for *backend*, or None when
    this target has no pinned engine (a declared gap in the table)."""
    from installation.registry import UnavailableOnTarget, pinned_file

    try:
        return pinned_file(backend_tool(backend)).version
    except (KeyError, UnavailableOnTarget, BinaryResolutionError):
        return None


def unavailable_reason(backend: str) -> str | None:
    """Why this target has no pinned engine for *backend*, if it hasn't.

    The pin table's declared gaps carry a sentence a user can act on;
    surfacing it beats "unavailable" with no explanation.
    """
    from installation.registry import UnavailableOnTarget, pinned_file

    try:
        pinned_file(backend_tool(backend))
    except UnavailableOnTarget as exc:
        return exc.reason
    except BinaryResolutionError as exc:
        return str(exc)
    except KeyError as exc:
        return str(exc)
    return None


def ensure_runtime_installed(backend: str) -> Path:
    """Provision the pinned engine for *backend* and return its directory.

    Idempotent: the provisioner keeps an entry already at its pin, so a
    second call costs a facts read. This is the DELIBERATE download path
    (the Local Models pane's button) — the boot ladder never calls it.
    """
    from installation.provisioner import provision_tool

    tool = backend_tool(backend)
    result = provision_tool(tool)
    if not result.ok:
        raise BinaryResolutionError(
            f"could not provision {tool}: {result.detail or result.action}"
        )
    directory = engine_dir(backend)
    if directory is None:
        raise BinaryResolutionError(
            f"{tool} reported {result.action} but no binary is recorded"
        )
    logger.info("llama.cpp engine %s ready (%s): %s", result.version, backend, directory)
    return directory


def verify_install(install_dir: Path) -> str:
    """The engine's own version banner, for display.

    The provisioner already proved the binary RUNS before recording it,
    so this is presentation, not verification.
    """
    exe = server_binary(install_dir)
    out = subprocess.run(
        [str(exe), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        cwd=str(exe.parent),
    )
    text = (out.stdout + out.stderr).strip()
    return text.splitlines()[0] if text else ""
