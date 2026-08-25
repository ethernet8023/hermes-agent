"""Extras: the runtime features of the python venv.

Feature names ARE pyproject extra names. This table maps each extra to the
import that proves it (the anchor), so availability is one find_spec — no
package-manager machinery on the hot path. sync_venv([extra]) makes an extra
true; pm owns HOW (uv sync inside the venv package).
"""

from __future__ import annotations

import importlib.util


# extra name -> module that proves it is installed
ANCHORS: dict[str, str | tuple[str, ...]] = {
    "anthropic": "anthropic",
    "bedrock": "boto3",
    "vertex": "google.auth",
    "azure-identity": "azure.identity",
    "exa": "exa_py",
    "firecrawl": "firecrawl",
    "parallel-web": "parallel",
    "otlp": "opentelemetry.sdk",
    "mistral": "mistralai",
    "edge-tts": "edge_tts",
    "tts-premium": "elevenlabs",
    "voice": "faster_whisper",
    "silk": "pilk",
    "wake": "openwakeword",
    "wake-tflite": "ai_edge_litert",
    "fal": "fal_client",
    "honcho": "honcho",
    "hindsight": "hindsight",
    "supermemory": "supermemory",
    "mem0": "mem0",
    "messaging": "telegram",
    "slack": "slack_bolt",
    "matrix": ("mautrix", "asyncpg", "aiosqlite", "markdown", "aiohttp_socks"),
    "dingtalk": "dingtalk_stream",
    "feishu": "lark_oapi",
    "wecom": "defusedxml",
    "teams": "microsoft.teams.apps",
    "modal": "modal",
    "daytona": "daytona",
    "vercel": "vercel",
    "google": "googleapiclient",
    "youtube": "youtube_transcript_api",
    "acp": "acp",
    "web": "fastapi",
    "doc-extract": "anydoc",
    "computer-use": "mcp",
    "trace-upload": "huggingface_hub",
}


def _anchors(extra: str) -> tuple[str, ...]:
    got = ANCHORS.get(extra, extra.replace("-", "_"))
    return got if isinstance(got, tuple) else (got,)


def _importable(anchor: str) -> bool:
    """An anchor already present in sys.modules counts even without a
    findable spec — tests fake SDKs by inserting modules there, and the
    caller's import right after this check resolves the same way."""
    import sys

    if anchor in sys.modules:
        return True
    try:
        return importlib.util.find_spec(anchor) is not None
    except (ImportError, ValueError):
        return False


def available(extra: str) -> bool:
    """Fast, side-effect-free: are all of this extra's anchors importable?"""
    return all(_importable(a) for a in _anchors(extra))


def ensure_import(extra: str) -> None:
    """Make an extra available: no-op when the anchor imports, otherwise
    sync the venv with the extra enabled. Raises InstallError on failure."""
    if available(extra):
        return
    from pm.ensure import sync_venv

    sync_venv([extra])


def ensure_and_bind(extra, importer, target_globals) -> bool:
    """ensure_import + rebind module-level names after a mid-process install.
    importer returns {name: value}; bound into target_globals on success."""
    try:
        ensure_import(extra)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning("extra %r unavailable: %s", extra, exc)
        return False
    try:
        bindings = importer()
    except ImportError as exc:
        import logging

        logging.getLogger(__name__).warning(
            "import after installing %r failed: %s", extra, exc
        )
        return False
    target_globals.update(bindings)
    return True


def missing(extra: str) -> tuple[str, ...]:
    return tuple(a for a in _anchors(extra) if not _importable(a))


# anchor package name (as it appears in install specs) -> owning extra
_SPEC_TO_EXTRA: dict[str, str] = {
    "honcho-ai": "honcho",
    "hindsight-client": "hindsight",
    "supermemory": "supermemory",
    "mem0ai": "mem0",
    "fastapi": "web",
    "uvicorn": "web",
}


class SpecInstallResult:
    def __init__(self, ok: bool, reason: str = "", stderr: str = "", command: str = "", stdout: str = ""):
        self.ok = ok
        self.reason = reason
        self.stderr = stderr
        self.stdout = stdout
        self.command = command

    @property
    def blocked(self) -> bool:
        return not self.ok and bool(self.reason)


def install_extra_for_specs(specs, timeout: int = 300) -> SpecInstallResult:
    # timeout is accepted for the old lazy_deps signature; the venv sync owns
    # its own subprocess timeout. Kept so call sites need no churn before the
    # spec-shaped interface dies (plugin deps become extras, plan step 5).
    """Install the extras that own the given package specs. The specs name
    packages that are pyproject extras' anchors; the venv package installs
    the exact locked versions — the spec's own pin is not consulted."""
    import re

    extras: set[str] = set()
    for spec in specs:
        pkg = re.split(r"[<>=!\[]", str(spec), 1)[0].strip().lower()
        extra = _SPEC_TO_EXTRA.get(pkg, pkg)
        extras.add(extra)
    try:
        from pm.ensure import sync_venv

        sync_venv(sorted(extras))
        return SpecInstallResult(
            True, command=f"uv sync --extra {' --extra '.join(sorted(extras))}"
        )
    except Exception as exc:
        from pm.package import InstallError

        if isinstance(exc, InstallError) and "lazy installs are disabled" in str(exc):
            return SpecInstallResult(False, reason=str(exc))
        return SpecInstallResult(False, stderr=str(exc))
