"""
Lazy dependency installer for opt-in Hermes Agent backends.

Many Hermes features (Mistral TTS, ElevenLabs TTS, Honcho memory, Bedrock,
Slack, Matrix, etc.) need Python packages that not every user wants. Each
one installs at first use, for two reasons. One quarantined or yanked
release on PyPI must not fail the whole resolve and cost a fresh install
ten unrelated extras. And a user who talks to one provider must not pull
hundreds of packages that they never import.

Backends call :func:`ensure` at the
top of their first-import path. If the deps are missing, ``ensure`` checks
the ``security.allow_lazy_installs`` config flag (default true) and runs
a venv-scoped pip install. If the user has explicitly disabled lazy
installs, ``ensure`` raises :class:`FeatureUnavailable` with a clear
remediation hint pointing at ``hermes tools`` or the manual pip command.

Security model:

* **Venv-scoped by default.** Installs target ``sys.executable`` in the
  active venv. We never touch the system Python.
* **Sealed deployments.** The Docker image sets
  ``HERMES_DISABLE_LAZY_INSTALLS=1`` and makes ``/opt/hermes`` read-only.
  Hermes refuses every install there. The image contains each extra that
  works in a container. A lazy install in the image means that the image
  does not have a dependency that it must ship.

* **Durable-target mode.** ``HERMES_LAZY_INSTALL_TARGET`` sends installs to
  a writable directory instead of the venv. The published image sets it to
  ``/opt/data/lazy-packages``, for :func:`install_specs` only: a plugin's
  packages come from its manifest, so no build can bake them. Hermes
  appends the directory to the END of ``sys.path``. It never prepends the
  directory, and it never exports ``PYTHONPATH``. The site-packages of the
  agent thus wins each name collision, and a package installed this way
  can only ADD modules.
* **PyPI by package name only.** Specs may be ``"package>=1.0,<2"`` etc.
  We do NOT support ``--index-url`` overrides, ``git+https://``, file:
  paths, or any other input that could be hijacked by a malicious config.
* **Allowlist.** Only specs that appear in :data:`LAZY_DEPS` can be
  installed via this path. A typo in feature name doesn't get the user
  install-anything semantics.
* **Opt-out.** Setting ``security.allow_lazy_installs: false`` in
  ``config.yaml`` disables runtime installs in BOTH modes. Users in
  restricted networks or strict security postures can pin themselves to
  whatever was installed at setup time.
* **Offline detection.** If the install fails (offline, mirror down,
  PyPI 404 / quarantine), we surface the failure as
  :class:`FeatureUnavailable` with the actual pip stderr — no silent
  retries, no caching of bad state.

Adding a new backend:

1. Add the packages as an extra in pyproject.toml, then map the feature
   to that extra in :data:`LAZY_DEPS`.
2. At the top of the backend module's import path, call
   ``ensure("feature.name")`` inside a try/except that converts
   :class:`FeatureUnavailable` to a useful runtime error.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional, Literal

from hermes_cli._subprocess_compat import windows_hide_flags

logger = logging.getLogger(__name__)


# =============================================================================
# Feature to extra map.
#
# Each key is a feature name with a dot ("namespace.backend"). Each value
# names the ``[project.optional-dependencies]`` extra in pyproject.toml
# that holds the packages for that backend: a plain string for a feature
# that works on every host, or a :class:`LazyDep` for a feature that
# carries its own host-support probe.
#
# pyproject.toml holds the specs. No other file holds them. Do not add a
# table of pins to this module. Such a table cannot read ``[tool.uv]
# override-dependencies``, so a backend that holds a security-pinned package
# below its patched version downgrades that package at first use.
# =============================================================================


class FeatureUnavailable(RuntimeError):
    """A lazily-installable feature is missing and cannot be made available.

    Either the deps were never installed and the user has disabled lazy
    installs, or the install attempt failed.

    The subclass IS the classification. The ``hermes update`` lazy-refresh
    pass reports an :class:`InstallSkipped` subtype as ``skipped:`` and a
    plain :class:`FeatureUnavailable` as ``failed:``. Raise the subtype
    that says why; never encode the classification in the message text.
    """

    def __init__(
        self,
        feature: str,
        missing: tuple[str, ...],
        reason: str,
        *,
        actionable: bool = True,
    ):
        self.feature = feature
        self.missing = missing
        self.reason = reason
        # Set this to False to remove the "install it yourself" footer. A
        # sealed Docker venv and a package-manager install are both
        # read-only, so the user cannot run the command. A command that
        # always fails is worse than no command.
        self.actionable = actionable
        super().__init__(self._format())

    def _format(self) -> str:
        base = f"Feature {self.feature!r} unavailable: {self.reason}"
        if not self.actionable or not self.missing:
            return base
        spec_list = " ".join(repr(s) for s in self.missing)
        return (
            f"{base}. "
            f"To enable manually: uv pip install {spec_list}  "
            f"(or: pip install {spec_list})."
        )


class InstallSkipped(FeatureUnavailable):
    """An expected refusal, not a broken install.

    The deployment or the user chose this outcome, so ``hermes update``
    reports it as ``skipped:`` and moves on. A broken install (pip
    failure, unreadable specs) stays a plain :class:`FeatureUnavailable`
    and reports as ``failed:``.
    """


class UnsupportedFeature(InstallSkipped):
    """This install can never run the feature.

    Raised by a :class:`LazyDep` ``supported`` probe (host capability:
    Matrix E2EE on native Windows), and by :func:`ensure` for a managed
    or read-only install that no runtime pip command can serve.

    Never actionable: a pip hint for an impossible install always fails,
    so the reason must carry the remedy (WSL, extraDependencyGroups, the
    owning package manager) itself.
    """

    def __init__(
        self,
        reason: str,
        *,
        feature: str = "",
        missing: tuple[str, ...] = (),
    ):
        super().__init__(feature, missing, reason, actionable=False)

    def _format(self) -> str:
        # A probe raises before any feature context exists; "Feature ''
        # unavailable" helps nobody.
        if not self.feature:
            return self.reason
        return super()._format()


class LazyInstallsDisabled(InstallSkipped):
    """The user set ``security.allow_lazy_installs: false``."""


class InstallDeclined(InstallSkipped):
    """The user answered no at the interactive install prompt."""


@dataclass(frozen=True)
class LazyDep:
    """A LAZY_DEPS entry that carries its own host-support probe.

    ``extra`` names the pyproject extra, exactly like a plain-string entry.

    ``supported`` is a platform capability gate, not a security policy
    gate. It raises :class:`UnsupportedFeature` when this host cannot
    run the feature, and returns None when it can. :func:`ensure` runs
    the probe before pip, so a known-impossible install never starts.

    A gate answers one question: can this host run the feature at all?
    A missing WHEEL is not that question and is not gated here — the
    runtime install forbids source builds and reports the gap uv finds,
    and the bundled build lane compiles the sdist on the target runner.
    See :func:`only_targets`.
    """

    extra: str
    supported: Callable[..., None]

PlatformType = Literal["linux", "win32", "darwin", "msys", "cygwin"]

#: Every target a Hermes artifact is built for, as pin-table keys. The
#: same spelling installation.registry.current_target() returns and the
#: desktop payload's own target table uses, so a gate here and a build
#: lane there name a host identically.
ALL_TARGETS = (
    "linux-x64",
    "linux-arm64",
    "darwin-x64",
    "darwin-arm64",
    "win32-x64",
    "win32-arm64",
)

#: What a target gate costs a host that cannot simply install a feature.
#: UNAVAILABLE: the feature cannot work on this target at all, because a
#: pinned package publishes nothing usable there and nothing can produce
#: it — not a user, not CI.
#:
#: There is no verdict for "no wheel here." A wheel gap is not a host
#: capability, it is a fact about an index that changes without anyone
#: editing this file, and a table restating it drifts the moment a
#: package publishes its first wheel or drops its sdist. The runtime
#: install passes uv `--no-build` and turns uv's own refusal into
#: :class:`UnsupportedFeature`, so the gap is reported by the one party
#: that reads the index every time.
UNAVAILABLE = "unavailable"


def _expand_target_keys(gates: dict[str, str]) -> dict[str, str]:
    """Expand a gate table's platform keys into per-target keys.

    A key is either a full target (``win32-arm64``) or a bare platform
    (``darwin``), which means every target of that platform. Raises for
    a key that is neither, so a typo fails at import instead of silently
    gating nothing.
    """
    expanded: dict[str, str] = {}
    for key, verdict in gates.items():
        if verdict != UNAVAILABLE:
            raise ValueError(
                f"only_targets: {key!r} has verdict {verdict!r}; "
                f"expected {UNAVAILABLE!r}"
            )
        matches = [t for t in ALL_TARGETS if t == key or t.startswith(f"{key}-")]
        if not matches:
            raise ValueError(
                f"only_targets: {key!r} matches no target "
                f"(expected a platform or one of {', '.join(ALL_TARGETS)})"
            )
        for target in matches:
            expanded[target] = verdict
    return expanded


def only_targets(gates: dict[str, str], explainer: str | None = None):
    """Gate a feature on the hosts named in *gates*.

    Keys are targets (``win32-arm64``) or whole platforms (``darwin``,
    expanded to every target of that platform). An unlisted target is
    unaffected: the packages install from published wheels like any
    other feature.

    Architecture is why this takes targets rather than platforms alone.
    A capability gap belongs to the (platform, arch) pair a package
    builds for, so gating the whole platform would take a working
    feature away from the arch that can run it.

    The one verdict is :data:`UNAVAILABLE`: the feature cannot work on
    this target for anyone. Nothing exists to install, so the refusal is
    the same for a user machine and for bundle staging, and the extra
    stays out of the artifact.

    A target whose packages merely lack a WHEEL does not belong here.
    Nothing in this file can know that reliably — it changes when an
    upstream project publishes, not when someone edits Hermes. The
    runtime install forbids source builds and turns uv's refusal into
    the same :class:`UnsupportedFeature`, naming the package uv named.
    """
    table = _expand_target_keys(gates)
    suffix = f" {explainer}" if explainer else ""

    def _supported() -> None:
        from installation.registry import current_target

        if table.get(current_target()) is None:
            return
        raise UnsupportedFeature(f"unsupported on {current_target()}.{suffix}")

    return _supported


def only_platform(platform: PlatformType, explainer: str | None = None): 
    def _supported():
        if sys.platform != platform:
            raise UnsupportedFeature(f"unsupported on platforms other than {platform}.{' ' + explainer if explainer else ''}")
    return _supported

def never_platform(platform: PlatformType, explainer: str | None = None):
    def _supported():
        if sys.platform == platform:
            raise UnsupportedFeature(f"unsupported on platform {platform}.{' ' + explainer if explainer else ''}")
    return _supported


LAZY_DEPS: dict[str, str | LazyDep] = {
    # ─── Inference providers ───────────────────────────────────────────────
    "provider.anthropic": "anthropic",
    "provider.bedrock": "bedrock",
    "provider.vertex": "vertex",
    "provider.azure_identity": "azure-identity",

    # ─── Web search backends ───────────────────────────────────────────────
    "search.exa": "exa",
    "search.firecrawl": "firecrawl",
    "search.parallel": "parallel-web",

    # ─── Monitoring ────────────────────────────────────────────────────────
    "export.otlp": "otlp",

    # ─── Speech to text ────────────────────────────────────────────────────
    # stt-whisper, not voice: this feature transcribes audio files, which
    # include voice notes that arrive over the network. It must not pull
    # the microphone stack in, and the Docker image bakes it.
    "stt.faster_whisper": LazyDep("stt-whisper", only_targets(
        {"darwin-x64": UNAVAILABLE, "win32-arm64": UNAVAILABLE},
        "faster-whisper needs ctranslate2 and onnxruntime. Neither publishes "
        "an sdist, and each one skips a target: ctranslate2 has no win_arm64 "
        "wheel, onnxruntime has no macOS x86_64 wheel. Use a cloud "
        "transcription backend instead.",
    )),
    "stt.mistral": "mistral",
    "stt.silk": "silk",

    # ─── Text to speech ────────────────────────────────────────────────────
    "tts.edge": "edge-tts",
    "tts.elevenlabs": "tts-premium",
    "tts.mistral": "mistral",

    # ─── Wake word engines ─────────────────────────────────────────────────
    "wake.openwakeword": LazyDep("wake-openwakeword", only_targets(
        {"darwin-x64": UNAVAILABLE},
        "openWakeWord runs on onnxruntime, which publishes no macOS x86_64 "
        "wheel and no sdist. Use wake.porcupine on an Intel Mac.",
    )),
    "wake.openwakeword.tflite": LazyDep("wake-tflite", only_targets(
        {"darwin-x64": UNAVAILABLE},
        "ai-edge-litert publishes no macOS x86_64 wheel and no sdist. The "
        "tflite path exists for Apple silicon.",
    )),
    "wake.sherpa": "wake-sherpa",
    "wake.porcupine": "wake-porcupine",

    # ─── Image generation backends ─────────────────────────────────────────
    "image.fal": "fal",

    # ─── Memory providers ──────────────────────────────────────────────────
    "memory.honcho": "honcho",
    "memory.hindsight": "hindsight",
    "memory.supermemory": "supermemory",
    "memory.mem0": LazyDep("mem0", only_targets(
        {"win32-arm64": UNAVAILABLE},
        "mem0 reaches grpcio through qdrant-client. grpcio publishes no "
        "win_arm64 wheel at any version, and its sdist cannot compile "
        "there: setup.py passes /std:c++17 and /std:c11 together and "
        "relies on a monkeypatch of Compiler.spawn to strip the wrong one "
        "per file, but current setuptools calls Compiler.call, so the "
        "filter never runs and cl fails with D8016. Use another memory "
        "provider on arm64 Windows.",
    )),

    # ─── Messaging platforms ───────────────────────────────────────────────
    "platform.telegram": "telegram",
    "platform.discord": "discord",
    "platform.slack": "slack",
    "platform.matrix": LazyDep("matrix", only_platform(
        "linux",
        "Matrix E2EE depends on python-olm, which only ships wheels for Linux."
        "Run Hermes under WSL to use Matrix on Windows."
    )),
    "platform.dingtalk": "dingtalk",
    "platform.feishu": "feishu",
    "platform.wecom_callback": "wecom",
    "platform.teams": "teams",

    # ─── Terminal backends ─────────────────────────────────────────────────
    "terminal.modal": "modal",
    "terminal.daytona": "daytona",
    "terminal.vercel": "vercel",

    # ─── Skills ────────────────────────────────────────────────────────────
    "skill.google_workspace": "google",
    "skill.youtube": "youtube",

    # ─── Tools ─────────────────────────────────────────────────────────────
    # [acp] has no entry here on purpose. The ACP entry point is a console
    # script, so its dependency must exist before the agent loop starts. It
    # ships in [all] instead, and an extra cannot be in both.
    "tool.dashboard": "web",
    "tool.computer_use": "computer-use",
    "tool.trace_upload": "trace-upload",
    "tool.doc_extract": "doc-extract",
}


# =============================================================================
# pyproject extra -> specs
# =============================================================================


def _project_root() -> Optional[Path]:
    """Return the root directory that holds pyproject.toml, or None.

    Hermes supports two install types. ``install.sh`` clones the repository,
    and the Docker image copies ``pyproject.toml`` and ``uv.lock`` to its
    WORKDIR. Each other layout, such as a copy in site-packages, has no
    project root. The extras table then comes from the dist metadata
    instead (see :func:`_metadata_optional_dependencies`).
    """
    root = Path(__file__).resolve().parent.parent
    return root if (root / "pyproject.toml").is_file() else None


@functools.lru_cache(maxsize=1)
def _pyproject() -> dict:
    """Parse pyproject.toml once, or return {} when it is not on disk.

    A Nix build puts the code in site-packages with no pyproject.toml beside
    it, so callers must handle an empty result.
    """
    root = _project_root()
    if root is None:
        return {}
    try:
        return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8-sig"))
    except Exception as e:
        logger.debug("Could not read pyproject.toml: %s", e)
        return {}


def _optional_dependencies() -> dict[str, tuple[str, ...]]:
    """Return ``[project.optional-dependencies]``.

    pyproject.toml is the primary source. On a checkout it is ahead of the
    installed dist metadata. A wheel install, such as Nix, does not have
    the file on disk. There the same table comes from the dist metadata.
    """
    raw = _pyproject().get("project", {}).get("optional-dependencies", {}) or {}
    if raw:
        return {k: tuple(v) for k, v in raw.items()}
    return _metadata_optional_dependencies()


# Finds the ``extra == "name"`` clause that setuptools appends to the marker
# of each Requires-Dist line that belongs to an extra.
_EXTRA_CLAUSE = re.compile(r"""\bextra\s*==\s*["']([^"']+)["']""")


@lru_cache(maxsize=1)
def _metadata_optional_dependencies() -> dict[str, tuple[str, ...]]:
    """The extras table, read from the installed dist metadata.

    A wheel install has no pyproject.toml beside the code. The dist-info
    carries the same table: each spec of an extra becomes one
    ``Requires-Dist`` line, and its marker holds ``extra == "name"``. A
    pin's own marker is ANDed on, for example ``platform_system ==
    "Darwin" and extra == "wake-tflite"``. Remove the extra clause and
    keep the rest of the marker.

    Without this fallback, each lazy_deps entry point raised on a Nix
    install. ensure() raised even for a feature whose packages were baked
    through extraDependencyGroups, and that call must be a no-op.
    """
    try:
        from importlib.metadata import metadata

        md = metadata("hermes-agent")
    except Exception as e:
        logger.debug("Could not read hermes-agent dist metadata: %s", e)
        return {}
    table: dict[str, list[str]] = {}
    for raw in md.get_all("Requires-Dist") or []:
        base, sep, marker = raw.partition(";")
        if not sep:
            continue  # core dependency — not part of any extra
        m = _EXTRA_CLAUSE.search(marker)
        if not m:
            continue
        rest = (marker[: m.start()] + marker[m.end() :]).strip()
        rest = re.sub(r"^\s*and\s+|\s+and\s*$", "", rest).strip()
        spec = base.strip() + (f"; {rest}" if rest else "")
        table.setdefault(m.group(1), []).append(spec)
    return {k: tuple(v) for k, v in table.items()}


_SELF_REF = re.compile(r"^hermes[-_]agent\[([^\]]+)\]$", re.IGNORECASE)


def extra_specs(extra: str, _seen: Optional[frozenset] = None) -> tuple[str, ...]:
    """Return the specs for ``extra`` and expand each ``hermes-agent[...]``.

    An extra can contain other extras. ``[messaging]`` contains
    ``hermes-agent[telegram]``, ``hermes-agent[discord]`` and
    ``hermes-agent[slack]``. This function expands each such reference. If
    the references make a loop, or point to an extra that does not exist,
    the function returns nothing and does not repeat forever.

    A marker belongs on the pin inside the extra that holds it, not on the
    reference. _is_satisfied reads the marker, so a spec for another
    platform needs no install here.
    """
    seen = _seen or frozenset()
    if extra in seen:
        logger.debug("Cyclic extra reference at %r — stopping", extra)
        return ()
    table = _optional_dependencies()
    if extra not in table:
        return ()
    seen = seen | {extra}
    out: list[str] = []

    def _add(spec: str) -> None:
        if spec not in out:
            out.append(spec)

    for spec in table[extra]:
        m = _SELF_REF.match(spec)
        if m:
            for sub in m.group(1).split(","):
                for nested in extra_specs(sub.strip(), seen):
                    _add(nested)
        else:
            _add(spec)
    return tuple(out)


def _anchor_spec(extra: str, _seen: Optional[frozenset] = None) -> Optional[str]:
    """Return the spec that identifies ``extra``: its first direct pin.

    ``extra_specs`` expands ``hermes-agent[...]`` references in place, so
    its first element can be a shared helper from a composed extra —
    ``[voice]`` starts with ``hermes-agent[audio-io]``, and expansion puts
    ``sounddevice`` first. sounddevice is in every audio extra, so it
    identifies none of them. The pin that identifies an extra is the first
    one written directly in it (``faster-whisper`` for ``[voice]``).

    Only when an extra holds nothing but references (``[computer-use]`` is
    ``hermes-agent[mcp]`` alone) does this recurse into the first reference.
    """
    seen = _seen or frozenset()
    if extra in seen:
        return None
    table = _optional_dependencies()
    if extra not in table:
        return None
    seen = seen | {extra}

    refs: list[str] = []
    for spec in table[extra]:
        m = _SELF_REF.match(spec)
        if m:
            refs.extend(sub.strip() for sub in m.group(1).split(","))
        else:
            return spec
    for ref in refs:
        found = _anchor_spec(ref, seen)
        if found:
            return found
    return None


@dataclass(frozen=True)
class _InstallResult:
    success: bool
    stdout: str
    stderr: str


# =============================================================================
# Internals
# =============================================================================


# Environment variable that sends lazy installs to a writable directory on a
# durable volume instead of the agent venv. The published image sets it to
# /opt/data/lazy-packages. There ensure() still refuses (the image bakes
# every extra it can run), so the directory serves install_specs alone.
# This is an internal bridge variable, not configuration for the user. The
# control for the user is security.allow_lazy_installs in config.yaml. When
# the variable is empty, lazy installs go into the active venv.
_LAZY_TARGET_ENV = "HERMES_LAZY_INSTALL_TARGET"

# Name of the stamp file written into the target dir recording the Python
# X.Y + ABI it was populated for. If a container rebuild bumps the
# interpreter, compiled wheels (.so) in the durable store would be ABI-
# incompatible; we detect the mismatch and wipe the store so packages get
# re-resolved against the new interpreter rather than importing a stale .so.
_TARGET_STAMP_NAME = ".python-abi"


def _python_abi_tag() -> str:
    """A stable token identifying the running interpreter's ABI.

    Combines the X.Y version with the EXT_SUFFIX (which encodes the ABI
    tag and platform, e.g. ``cpython-313-x86_64-linux-gnu``). Two
    interpreters that can share compiled wheels produce the same token.
    """
    ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    ext = sysconfig.get_config_var("EXT_SUFFIX") or ""
    return f"{ver}:{ext}"


def _lazy_install_target() -> Optional[Path]:
    """Return the durable install-target dir, or None for venv-scoped mode.

    Resolution order (doc4 §B):

    1. :data:`_LAZY_TARGET_ENV` — the explicit override. Docker keeps it
       one release as the grandfathered bridge (its /opt/data anchor
       predates the state folder); the desktop bridge dies with §A's
       Electron work.
    2. Sealed tree → ``installs/<SHA16>/lazy-packages`` in the state
       folder. A sealed venv can never take a venv-scoped install, so
       the derived overlay is not an option there — it is the lane.
    3. Checkout → None (venv-scoped mode; the venv IS the writable
       store, and deleting the checkout deletes the packages with it).
    """
    raw = os.environ.get(_LAZY_TARGET_ENV, "").strip()
    if raw:
        return Path(raw)
    try:
        from hermes_cli.boot_bootstrap import ensure_install_dir
        from hermes_constants import get_install_root
        from installation.tree import Sealed, runtime_tree

        root = get_install_root()
        if isinstance(runtime_tree(root), Sealed):
            return ensure_install_dir(root) / "lazy-packages"
    except Exception:  # noqa: BLE001 — derivation must not block an install
        logger.debug("lazy target derivation failed", exc_info=True)
    return None


def _site_packages_writable() -> bool:
    """Can venv-scoped installs write to this interpreter's site-packages?

    False for read-only stores (a Nix-built venv, or any distro shipping
    Hermes from an immutable path). A probe of the real directory beats
    inferring the packager: it is true for every read-only layout, current
    and future. Errs toward True — the install ladder itself reports write
    failures with full context.
    """
    try:
        site_packages = sysconfig.get_paths()["purelib"]
    except (KeyError, OSError):
        return True
    try:
        return os.access(site_packages, os.W_OK)
    except OSError:
        return True


def _ensure_target_ready(target: Path) -> Optional[str]:
    """Create the target dir and validate its ABI stamp.

    If the stamp is missing it is written. If it is present but records a
    different interpreter ABI than the one now running (e.g. the container
    image was rebuilt onto a newer Python), the directory's contents are
    wiped and the stamp rewritten, so stale compiled wheels can't be
    imported against an incompatible interpreter.

    Returns ``None`` on success, or an error string if the directory can't
    be created / written (e.g. read-only mount, permission error).
    """
    want = _python_abi_tag()
    stamp = target / _TARGET_STAMP_NAME
    try:
        if target.exists():
            have = ""
            try:
                have = stamp.read_text(encoding="utf-8-sig").strip()
            except (OSError, FileNotFoundError):
                have = ""
            if have and have != want:
                logger.info(
                    "Lazy install target %s was built for ABI %r but running "
                    "ABI is %r; wiping stale packages.",
                    target, have, want,
                )
                for child in target.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        try:
                            child.unlink()
                        except OSError:
                            pass
        target.mkdir(parents=True, exist_ok=True)
        stamp.write_text(want, encoding="utf-8")
    except OSError as e:
        return f"lazy install target {target} is not writable: {e}"
    return None


def _activate_target_on_syspath(target: Path) -> None:
    """Append the durable target to ``sys.path`` so its packages import.

    Appended to the END (never prepended) so the agent's own venv
    site-packages takes precedence on every name collision. Idempotent.
    Uses :func:`site.addsitedir` so ``.pth`` files (namespace packages,
    editable installs) inside the target are honoured, then enforces the
    append ordering — ``addsitedir`` would otherwise insert near the front.
    """
    target_str = str(target)
    # Snapshot existing entries so we can restore precedence afterwards.
    before = list(sys.path)
    if target_str not in before:
        site.addsitedir(target_str)
    # site.addsitedir may have inserted target (and any .pth-added dirs) at
    # the front. Move every newly-added entry to the end, preserving the
    # core venv's precedence. New entries are those not present `before`.
    new_entries = [p for p in sys.path if p not in before]
    if new_entries:
        sys.path[:] = [p for p in sys.path if p not in new_entries] + new_entries
    # importlib.metadata caches the path-based distribution finder; clear it
    # so a just-activated dir is visible to version() checks this process.
    try:
        import importlib
        importlib.invalidate_caches()
    except Exception:
        pass


def activate_durable_lazy_target() -> None:
    """Public: wire the durable lazy-install target onto ``sys.path``.

    Safe no-op when :data:`_LAZY_TARGET_ENV` is unset or the directory does
    not yet exist. Called once early in process startup (before backends
    import) so packages installed into the durable store on a previous run
    are importable on this run. Never raises.
    """
    target = _lazy_install_target()
    if target is None:
        return
    try:
        if target.exists():
            _activate_target_on_syspath(target)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Failed to activate durable lazy target %s: %s", target, e)


# One wording for the config kill switch. ensure() and install_specs both
# report it, and two spellings of the same cause read like two causes.
_CONFIG_DISABLED_REASON = "lazy installs disabled (security.allow_lazy_installs=false)"


def managed_install_reason(feature: str, extra: Optional[str] = None) -> str:
    """Return the message for an install that this deployment cannot run.

    Each caller reaches this when Hermes cannot install a package at run
    time. The remedy differs by deployment, so name the deployment and give
    the command or option that works there.

    ``extra`` is the pyproject extra that holds the packages, when the
    caller knows it. The NixOS remedy needs that name.

    Public on purpose: plugin setup flows (google_chat, honcho) report the
    same remedies when their own install paths cannot run.
    """
    # Check the package manager first. A managed install can also carry
    # HERMES_DISABLE_LAZY_INSTALLS, and the remedy for that user is the
    # package manager, not a bug report about a container image.
    #
    # get_managed_system() returns the string "NixOS" for each Nix install.
    # That value is an identifier, not a platform: `nix profile install` and
    # nix-darwin give the same value on a host that does not run NixOS. The
    # message below therefore says Nix, and gives both ways to set the
    # option.
    managed_by = _managed_system()
    if managed_by == "NixOS":
        target = f'"{extra}"' if extra else "the extra for this feature"
        return (
            "this build comes from Nix, and the /nix/store is read-only, so "
            f"Hermes cannot install packages at run time. Add {target} to "
            "extraDependencyGroups and rebuild. That option puts the extra "
            "into the sealed venv. On NixOS, set "
            "services.hermes-agent.extraDependencyGroups. Elsewhere, use "
            "pkgs.hermes-agent.override { extraDependencyGroups = [ ... ]; }. "
            "For a package that pyproject.toml does not declare, use "
            "extraPythonPackages instead."
        )
    if managed_by:
        return (
            f"this build comes from {managed_by}, so Hermes cannot install "
            f"packages at run time. Add the dependencies for {feature!r} "
            f"through {managed_by}."
        )

    if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1":
        return (
            "runtime dependency installs are disabled in this deployment "
            "(HERMES_DISABLE_LAZY_INSTALLS=1). The container image contains "
            "each backend that it can run, so this is probably a bug in the "
            "image build. Please report it. Do not install the package into "
            "the container. /opt/hermes is read-only, and the next image "
            "update removes the change."
        )

    return _CONFIG_DISABLED_REASON


def _managed_system() -> str:
    """Return the name of the package manager that owns this install."""
    try:
        from hermes_cli.config import get_managed_system

        return get_managed_system() or ""
    except Exception:
        return ""


def _sealed_venv_reason() -> Optional[str]:
    """Return why a sealed deployment refuses an install, or None."""
    if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") != "1":
        return None
    return managed_install_reason("", None)


def _allow_lazy_installs() -> bool:
    """Return whether lazy installs are permitted in this environment.

    Hermes reads two controls, in this order:

    1. ``security.allow_lazy_installs: false`` in config.yaml. This is the
       control for the user, and it stops every install.
    2. ``HERMES_DISABLE_LAZY_INSTALLS=1``, which the Docker image sets. This
       control also stops every install. The image contains each extra that
       works in a container, so no correct install remains at run time.

    The default is True. If Hermes cannot read the config, it permits the
    install. A refusal locks the user out of a backend that the user owns,
    so the user must select the refusal.
    """
    # (1) Config kill switch wins in every mode.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception:
        cfg = None
    if cfg is not None:
        sec = cfg.get("security") or {}
        if not bool(sec.get("allow_lazy_installs", True)):
            return False

    # (2) Sealed deployment. The image contains each extra that a container
    # can run, so a LAZY_DEPS feature never needs an install there.
    #
    # install_specs is different. Its specs come from a plugin manifest, and
    # a plugin outside this repository declares packages that pyproject.toml
    # does not hold, so the image cannot have baked them. Hindsight appends
    # `hindsight-all` at setup time for the same reason. Sealing those off
    # would stop a user installing a memory provider in the container at all.
    # HERMES_LAZY_INSTALL_TARGET names a writable directory on the data
    # volume for exactly that case.
    if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1":
        return _lazy_install_target() is not None

    return True


def feature_extra(feature: str) -> str:
    """Return the pyproject extra that ``feature`` maps to.

    Raises KeyError for an unknown feature, exactly like ``LAZY_DEPS[...]``
    did when every value was a string.
    """
    entry = LAZY_DEPS[feature]
    return entry.extra if isinstance(entry, LazyDep) else entry


def check_supported(feature: str) -> None:
    """Run the host-support probe of ``feature``, when it has one.

    Raises :class:`UnsupportedFeature` when this host cannot run the
    feature. Returns None for a supported feature, for a plain-string
    entry, and for an unknown feature (:func:`ensure` owns the allowlist
    check, and reports that error better).

    This is a platform capability gate, not a security policy gate. It
    keeps known-impossible installs out of both first-use lazy
    installation and the ``hermes update`` lazy-refresh pass.

    One question, one answer, for every asker: a gate that passes here
    passes for the bundled build lane too. A missing wheel is not asked
    about — :func:`ensure` finds that during the install itself.
    """
    entry = LAZY_DEPS.get(feature)
    if isinstance(entry, LazyDep):
        entry.supported()


def _parse_spec(spec: str):
    """Parse a PEP 508 spec, or return None when it is not usable.

    ``packaging`` is a core dependency, so use it. A regex over a spec has
    to re-handle the extras block, the version set and the environment
    marker, and getting the marker wrong makes a specifier unparseable
    ("==2.1.6; platform_system == 'Darwin'").

    Import it here, not at module scope. hermes_bootstrap imports this
    module during startup, before a broken venv has been repaired, and a
    missing package must not stop Hermes from starting.
    """
    try:
        from packaging.requirements import InvalidRequirement, Requirement
    except ImportError:  # pragma: no cover - packaging is a core dependency
        return None
    try:
        return Requirement(spec)
    except InvalidRequirement:
        return None


def _pkg_name_from_spec(spec: str) -> str:
    """Return the bare package name, or the input when it does not parse."""
    req = _parse_spec(spec)
    return req.name if req else spec


def _is_satisfied(spec: str) -> bool:
    """Is ``spec`` already met in this environment?

    Checks the version, not only presence, so `hermes update` carries a pin
    bump to a backend that a user installed at an older version.

    A spec whose marker is false for this host counts as met. There is
    nothing to install: ``ai-edge-litert`` is for macOS, and asking pip for
    it on Linux gets an error, not a package.

    ``SpecifierSet.contains`` covers the rest. An empty specifier accepts
    any version, and a version string it cannot read gives False, which
    reinstalls and repairs the entry.
    """
    req = _parse_spec(spec)
    if req is None:
        return True
    if req.marker is not None and not req.marker.evaluate():
        return True

    from importlib.metadata import version

    try:
        installed = version(req.name)
    except Exception:
        # PackageNotFoundError is the normal miss; anything else (broken
        # dist-info metadata) also means "not usable, reinstall".
        return False
    return req.specifier.contains(installed, prereleases=True)


def _is_present(spec: str) -> bool:
    """Is the package installed, at any version?

    :func:`active_features` uses this to find the backends that a user
    turned on. A moved pin must still count as active, so drop the version
    and ask only about the name.
    """
    return _is_satisfied(_pkg_name_from_spec(spec))


def _run(
    cmd: list[str],
    *,
    timeout: int,
    env: Optional[dict] = None,
    check: bool = False,
):
    """Run ``cmd`` and capture its output.

    One place for the flags each install command needs: capture the output,
    decode it without raising on a byte that does not fit the locale, give
    the child no stdin so a prompt cannot hang the agent, and hide the
    console window on Windows.

    Call ``subprocess.run`` through the module attribute. The tests replace
    that attribute to read the argv, so an imported ``run`` would bypass
    them.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        check=check,
        stdin=subprocess.DEVNULL,
        creationflags=windows_hide_flags(),
    )


def _write_temp_requirements(lines, prefix: str) -> Optional[Path]:
    """Write ``lines`` to a temporary requirements file and return its path.

    Returns None for an empty list, and None when the write fails. Each
    caller treats None as "run the install without this file".
    """
    lines = list(lines)
    if not lines:
        return None
    try:
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return Path(path)
    except Exception as e:
        logger.debug("Could not write %s file: %s", prefix, e)
        return None


def _core_constraints_file() -> Optional[Path]:
    """Write a pip constraints file pinning every package already importable
    in the core environment to its installed version.

    Passed as ``--constraint`` for durable-target installs so the resolver
    pins shared transitive deps (httpx, pydantic, aiohttp, …) to the exact
    versions the core venv already ships, instead of pulling newer copies
    into the durable store. Two payoffs:

    * The durable store stays minimal — only genuinely-new packages land
      there; shared deps resolve to "already satisfied" against core.
    * A backend that *requires* a version conflicting with core fails loudly
      at install time (resolver conflict) rather than silently installing a
      shadowed copy that can never win on sys.path anyway.

    Returns the path to a temp constraints file, or None if enumeration
    failed (in which case the caller installs without constraints — still
    safe, just less tidy).
    """
    try:
        from importlib.metadata import distributions
    except ImportError:
        return None
    try:
        lines = []
        seen = set()
        for dist in distributions():
            name = dist.metadata["Name"] if dist.metadata else None
            ver = dist.version
            if not name or not ver:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{name}=={ver}")
        return _write_temp_requirements(sorted(lines), "hermes-core-constraints-")
    except Exception as e:
        logger.debug("Could not build core constraints file: %s", e)
        return None


# Hermes applies these overrides to each lazy install. They repeat
# ``[tool.uv] override-dependencies`` in pyproject.toml.
#
# ``uv pip install`` and ``pip install`` do not read ``[tool.uv]``. Thus a
# transitive dependency can hold a security-pinned package below its patched
# version, and the first use of that backend downgrades the core venv.
#
# Example, measured with cryptography. The core venv has 50.0.0. The user
# enables DingTalk, which needs ``alibabacloud-dingtalk``, which needs
# ``alibabacloud-tea-openapi==0.4.5``, which holds ``cryptography<49``. The
# install gives::
#
#     + cryptography==48.0.1     # three open advisories, again
#
# A pin next to the specs does not correct this. The resolver obeys the pin
# and moves ``alibabacloud-tea-openapi`` back to 0.3.16, an sdist build from
# two years ago. A pin on both packages has no solution. Only an overrides
# file keeps the patched version and the correct backend version together,
# so Hermes gives the file to the uv tier below.
@lru_cache(maxsize=1)
def _security_overrides() -> tuple[str, ...]:
    """Return ``[tool.uv] override-dependencies`` from pyproject.toml.

    Read the list, instead of duplicating, to avoid drift.
    """
    raw = (
        _pyproject().get("tool", {}).get("uv", {}).get("override-dependencies", [])
        or []
    )
    return tuple(str(s) for s in raw)


def _security_overrides_file() -> Optional[Path]:
    """Write the overrides to a temporary file for ``--overrides``.

    Returns the path, or None when Hermes cannot write the file. The caller
    then installs without the overrides, and the downgrade that these
    prevent becomes possible again.
    """
    return _write_temp_requirements(
        _security_overrides(), "hermes-lazy-overrides-"
    )


def _uv_sync_extra(feature: str) -> Optional[_InstallResult]:
    """Install the extra of ``feature`` with ``uv sync``.

    Hermes tries ``uv sync`` first. It is the only installer that reads
    ``uv.lock`` and applies ``[tool.uv] override-dependencies``. It thus
    installs the versions that CI examined, and it applies the security
    overrides that ``uv pip`` and ``pip`` cannot read.

    Returns None in these conditions, and the caller then uses the pip
    tiers:

    * A durable install target is active. That mode installs to a different
      directory, so that it cannot change the sealed venv. ``uv sync``
      controls a full venv and has no equal to ``--target``.
    * There is no project root that holds ``uv.lock`` and ``pyproject.toml``.
    * uv is not available.
    * pyproject.toml does not declare the extra of the feature.

    The ``--inexact`` flag is necessary. A plain ``uv sync`` removes each
    package outside the extras that it syncs, and this removes every other
    backend that the user enabled. The ``--no-install-project`` flag stops
    uv from installing Hermes over an editable checkout.
    """
    if _lazy_install_target() is not None:
        return None
    root = _project_root()
    if root is None or not (root / "uv.lock").is_file():
        return None
    if feature not in LAZY_DEPS:
        return None
    extra = feature_extra(feature)
    if extra not in _optional_dependencies():
        return None

    try:
        from installation.uv import uv_path

        resolved = uv_path()
        uv_bin = str(resolved) if resolved is not None else None
    except Exception:
        uv_bin = None
    if not uv_bin:
        return None

    try:
        from tools.environments.local import hermes_subprocess_env

        env = hermes_subprocess_env(inherit_credentials=False)
    except Exception:
        env = dict(os.environ)
    # uv sync targets the project environment; point it at the running venv so
    # a lazy install lands where the agent will import from.
    env["UV_PROJECT_ENVIRONMENT"] = str(Path(sys.executable).parent.parent)
    # --locked needs [tool.uv] visible; UV_NO_CONFIG would drop exclude-newer.
    env.pop("UV_NO_CONFIG", None)

    cmd = [
        uv_bin, "sync",
        # uv finds the project from its own working directory. The agent
        # runs from the user's working directory, not from the install
        # tree, so name the project. Without this flag the sync fails in
        # the wrong directory and this tier never runs.
        "--project", str(root),
        "--extra", extra,
        "--inexact",
        "--locked",
        "--no-install-project",
        "--python", sys.executable,
        # A user machine is not a build machine. Without this uv compiles
        # any package that publishes no wheel for this host, and the user
        # meets a compiler error mid-conversation instead of a refusal
        # naming the package. ensure() turns the refusal into
        # UnsupportedFeature.
        "--no-build",
    ]
    try:
        r = _run(cmd, timeout=600, env=env)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.debug("uv sync unavailable (%s) — falling back to pip ladder", e)
        return None
    if r.returncode == 0:
        logger.info("Installed extra [%s] for feature %r via uv sync", extra, feature)
        return _InstallResult(True, r.stdout or "", r.stderr or "")
    # A stale lockfile (--locked refuses) or any other sync failure falls back
    # rather than hard-failing: the pip ladder can still install the specs.
    logger.debug(
        "uv sync --extra %s failed (rc=%d), falling back: %s",
        extra, r.returncode, (r.stderr or "").strip()[:300],
    )
    return None


def _venv_pip_install(specs: tuple[str, ...], *, timeout: int = 300) -> _InstallResult:
    """Install ``specs`` via the shared ladder (installation.pip_ladder).

    Two modes:

    * **Venv-scoped (default).** Installs into the active venv
      (``sys.executable``). Used on normal installs.
    * **Durable-target.** When :data:`_LAZY_TARGET_ENV` is set, installs into
      that directory via ``--target`` and constrains shared deps to the
      core venv's versions (see :func:`_core_constraints_file`). The target
      is append-only on ``sys.path`` so it can never shadow core. Used by
      the immutable Docker image to keep lazy installs off the sealed venv.

    Lazy-install policy carried into the ladder as arguments (this used
    to be the second of three divergent copies of the mechanics):
    ``uv_path()`` and never ``ensure_uv()``. This runs mid-turn to
    satisfy an optional import, and downloading uv as a side effect of
    that is a far bigger action than the caller asked for. An
    unprovisioned tree fails with the provisioner hint instead.
    """
    if not specs:
        return _InstallResult(True, "", "")

    target = _lazy_install_target()
    constraints: Optional[Path] = None

    if target is not None:
        err = _ensure_target_ready(target)
        if err:
            return _InstallResult(False, "", err)
        constraints = _core_constraints_file()

    overrides = _security_overrides_file()

    try:
        from tools.environments.local import hermes_subprocess_env

        env = hermes_subprocess_env(inherit_credentials=False)

        try:
            from installation.uv import uv_path

            resolved = uv_path()
            uv_bin = str(resolved) if resolved is not None else None
        except Exception:
            uv_bin = None

        from installation.pip_ladder import pip_install

        result = pip_install(
            specs,
            uv_bin=uv_bin,
            timeout=timeout,
            target=target,
            constraints=constraints,
            overrides=overrides,
            env=env,
            creationflags=windows_hide_flags(),
            # See the same flag on the uv sync tier: no compiling on a
            # user machine. ensure() reads the refusal back with
            # pip_ladder.wheel_gap().
            no_build=True,
        )
        if result.ok and target is not None:
            _activate_target_on_syspath(target)
        return _InstallResult(result.ok, result.stdout, result.stderr)
    finally:
        for tmp in (constraints, overrides):
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass


# =============================================================================
# Public API
# =============================================================================


def feature_specs(feature: str) -> tuple[str, ...]:
    """Return the specs for ``feature``, read from its pyproject extra.

    Raises KeyError for an unknown feature, and FeatureUnavailable if the
    feature maps to an extra that pyproject doesn't define (a mapping typo, or
    a stripped install with no pyproject) — failing loudly beats installing
    nothing and reporting success.
    """
    extra = feature_extra(feature)
    specs = extra_specs(extra)
    if not specs:
        raise FeatureUnavailable(
            feature,
            (),
            f"feature {feature!r} maps to extra [{extra}], which resolved to no "
            f"packages. Either [{extra}] does not exist, or neither "
            f"pyproject.toml (root: {_project_root()!r}) nor the hermes-agent "
            f"dist metadata is readable here.",
        )
    return specs


def feature_missing(feature: str) -> tuple[str, ...]:
    """Return the subset of specs for ``feature`` not currently installed."""
    return tuple(s for s in feature_specs(feature) if not _is_satisfied(s))


def ensure(feature: str, *, prompt: bool = True) -> None:
    """Make sure all packages for ``feature`` are importable.

    If they're missing, attempts to install them in the active venv. Raises
    :class:`FeatureUnavailable` if the user has disabled lazy installs or
    if the install attempt fails.

    ``prompt``: when True (default) and stdin is a TTY, asks the user to
    confirm before installing. Non-interactive callers (gateway, cron,
    batch) get prompt=False and skip the confirmation — config flag is
    the gate in that case.
    """
    if feature not in LAZY_DEPS:
        raise FeatureUnavailable(
            feature, (), f"feature {feature!r} not in LAZY_DEPS allowlist"
        )

    missing = feature_missing(feature)
    if not missing:
        # The backend is in use with everything installed. Record the use,
        # so `hermes update` refreshes this feature when a pin moves.
        _record_feature_use(feature)
        return

    try:
        check_supported(feature)
    except UnsupportedFeature as e:
        raise UnsupportedFeature(e.reason, feature=feature, missing=missing) from e

    # A read-only site-packages (any nix build, or any other distro that
    # ships Hermes from a read-only store) cannot receive lazy pip installs:
    # the uv -> pip -> ensurepip ladder below burns ~15s bootstrapping
    # ensurepip only to fail on the read-only target. Probe writability
    # directly and fail fast with an actionable message instead — no
    # install-method inference needed.
    #
    # Skipped when a durable install target is configured: the container
    # deployment sets HERMES_LAZY_INSTALL_TARGET (a writable volume), where
    # lazy installs legitimately work.
    if _lazy_install_target() is None:
        managed_by = _managed_system()
        if managed_by:
            raise UnsupportedFeature(
                "unsupported on a managed install: "
                + managed_install_reason(feature, feature_extra(feature)),
                feature=feature,
                missing=missing,
            )
        if not _site_packages_writable():
            # Not a recognized managed system, but the store is still
            # read-only (a raw nix profile, a distro package without
            # HERMES_MANAGED set). Same dead-end, generic remedy.
            raise UnsupportedFeature(
                "unsupported on read-only installs: this build's "
                "site-packages is not writable (e.g. a Nix store path), so "
                "Hermes cannot install packages at runtime. Add the "
                f"dependencies for {feature!r} through the package manager "
                "that installed Hermes.",
                feature=feature,
                missing=missing,
            )

    # The explicit user opt-out outranks the sealed-image diagnosis: an
    # operator who set security.allow_lazy_installs=false asked for a
    # quiet "skipped", not a container-bug report.
    if not _allow_lazy_installs():
        raise LazyInstallsDisabled(feature, missing, _CONFIG_DISABLED_REASON)

    # A sealed image contains each extra that a container can run, so a
    # LAZY_DEPS feature must never install here, even when a durable target
    # exists. That target is for install_specs, whose packages come from a
    # plugin manifest and cannot be in the image.
    sealed = _sealed_venv_reason()
    if sealed is not None:
        raise FeatureUnavailable(feature, missing, sealed, actionable=False)

    # Only show the interactive confirmation when we own a TTY and
    # prompt_toolkit isn't running.  A bare input() deadlocks when a
    # prompt_toolkit app owns the terminal because keystrokes route to
    # its event loop rather than stdin, so the prompt blocks forever.
    # Under the TUI we skip the prompt and proceed — lazy installs are
    # gated by security.allow_lazy_installs, so reaching here is
    # already user opt-in.
    _pt_active = False
    if "prompt_toolkit.application.current" in sys.modules:
        try:
            from prompt_toolkit.application.current import get_app_or_none
            _app = get_app_or_none()
            _pt_active = _app is not None and getattr(_app, "is_running", False)
        except Exception:
            _pt_active = False

    if prompt and not _pt_active and sys.stdin.isatty() and sys.stdout.isatty():
        spec_list = ", ".join(missing)
        try:
            answer = input(
                f"\nFeature {feature!r} requires: {spec_list}\n"
                f"Install into the active venv now? [Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer and answer not in {"y", "yes"}:
            raise InstallDeclined(
                feature, missing, "user declined install at prompt"
            )

    logger.info("Lazy-installing %s for feature %r", " ".join(missing), feature)
    # Tier 0: `uv sync --extra <name>`, which resolves against uv.lock and
    # honours [tool.uv] override-dependencies. This is the only installer that
    # reproduces exactly what CI audited, so it is tried before the
    # pip-compatible ladder. Needs a project root + lockfile, and cannot serve
    # durable-target mode (it manages a venv wholesale, and the sealed-venv
    # image redirects installs to a separate dir on purpose).
    result = _uv_sync_extra(feature)
    if result is None:
        result = _venv_pip_install(missing)
    if not result.success:
        # One failure cause gets its own error: the install was refused
        # because a package publishes no wheel for this host. That is a
        # host capability answer, identical in kind to a target gate, so
        # it is reported as one rather than as a wall of resolver output.
        # uv names the package, so the message can too — and because the
        # index is read live, the answer is right on the day it is asked
        # and cannot go stale in a table here.
        from installation.pip_ladder import LadderResult, wheel_gap

        gap = wheel_gap(
            LadderResult(False, result.stdout or "", result.stderr or "", "uv")
        )
        if gap:
            raise UnsupportedFeature(
                f"unsupported on this machine: {gap} publishes no prebuilt "
                f"wheel for it, and Hermes does not compile packages on your "
                f"machine.",
                feature=feature,
                missing=missing,
            )
        # Surface the actual pip error so the user can debug PyPI-side
        # issues (404 quarantine, network down, etc.).
        snippet = (result.stderr or result.stdout or "").strip()
        if snippet:
            # Clip to a readable size — pip can dump pages of resolution traces.
            snippet = snippet[-2000:]
        raise FeatureUnavailable(
            feature, missing,
            f"pip install failed: {snippet or 'no error output'}"
        )

    # Verify post-install. importlib.metadata caches per-process, so if we
    # just installed something the cache may not see it without a refresh.
    try:
        import importlib.metadata as _md
        if hasattr(_md, "_cache_clear"):
            _md._cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass

    still_missing = feature_missing(feature)
    if still_missing:
        raise FeatureUnavailable(
            feature, still_missing,
            "install reported success but packages still not importable "
            "(may require Python restart)"
        )

    logger.info("Lazy install complete for feature %r", feature)
    _record_feature_use(feature)


def is_available(feature: str) -> bool:
    """Return True if the feature's deps are already satisfied.

    Never raises. Callers use this in status displays and in registry
    ``check_fn``s, and an exception there kills the caller. When the specs
    are unreadable — no pyproject and no dist metadata — the answer is
    False, not an error.
    """
    if feature not in LAZY_DEPS:
        return False
    try:
        return not feature_missing(feature)
    except Exception as e:
        logger.debug("is_available(%r): specs unreadable: %s", feature, e)
        return False


def feature_install_command(feature: str, *, venv_pip: bool = False) -> Optional[str]:
    """Return the ``pip install`` command a user could run manually, or None.

    ``venv_pip=True`` targets the running interpreter's pip
    (``{sys.executable} -m pip install …``) — correct in every layout
    (default install, ``HERMES_HOME`` overrides, profile installs) and
    immune to Ubuntu 24.04's PEP 668 ``externally-managed-environment``
    failure that a bare/system ``pip install`` hint invites.  The default
    ``uv pip install`` form is kept for contexts that document uv usage.

    Never raises. The contract is Optional[str], and callers put the
    result into hint strings with no try/except.
    """
    if feature not in LAZY_DEPS:
        return None
    try:
        specs = feature_specs(feature)
    except Exception:
        return None
    joined = " ".join(repr(s) for s in specs)
    if venv_pip:
        return f"{sys.executable} -m pip install {joined}"
    return "uv pip install " + joined


@dataclass
class InstallSpecsResult:
    """Outcome of :func:`install_specs` for one batch of pip specs.

    ``ok``       — install succeeded (or nothing was missing).
    ``blocked``  — installs are gated off (config kill switch, sealed venv
                   without a durable target) or a spec failed validation;
                   nothing was executed. ``reason`` explains why.
    ``command``  — human-readable description of what ran (for UIs/logs).
    """
    ok: bool
    blocked: bool = False
    reason: str = ""
    command: str = ""
    stdout: str = ""
    stderr: str = ""


def install_specs(specs: list[str] | tuple[str, ...], *, timeout: int = 300) -> InstallSpecsResult:
    """Install arbitrary (validated) pip specs through the lazy-install pipeline.

    This is the environment-aware install path for callers whose package
    lists come from data (e.g. memory-provider plugin manifests declaring
    ``pip_dependencies``) rather than the static :data:`LAZY_DEPS` allowlist.
    It applies the exact same environment routing as :func:`ensure`:

    * **Venv-scoped by default** — installs into ``sys.executable``'s venv.
    * **Durable-target on immutable images** — when the deployment seals the
      agent venv (``HERMES_DISABLE_LAZY_INSTALLS=1``) and sets
      ``HERMES_LAZY_INSTALL_TARGET``, installs are redirected to the writable
      data-volume dir (``--target`` + core-venv constraints), then activated
      on ``sys.path`` so the packages import in this process immediately.
    * **Gated** — honors ``security.allow_lazy_installs`` and refuses to run
      when the venv is sealed with no durable target (never attempts a write
      to a read-only tree; reports *why* instead of surfacing EROFS/EACCES).

    Unlike :func:`ensure`, a package outside pyproject.toml is permitted.
    Hindsight appends ``hindsight-all`` at setup time, and a plugin outside
    this repository declares its own packages, so a list of permitted names
    cannot work here.

    This function does NOT check the shape of a spec, and a check would give
    nothing. The specs come from ``plugin.yaml``, and the same file holds
    ``external_dependencies[].install``, which
    hermes_cli/web_server.py runs through ``subprocess.run(shell=True)``. The
    plugin's ``__init__.py`` runs as well, at import. Anyone who can write
    that manifest already runs code as the user, so a pattern that rejects
    ``--index-url`` protects nothing.

    Never raises; inspect the returned :class:`InstallSpecsResult`.
    """
    cleaned = tuple(str(s).strip() for s in specs if str(s).strip())
    if not cleaned:
        return InstallSpecsResult(ok=True, command="")

    if not _allow_lazy_installs():
        reason = _sealed_venv_reason() or _CONFIG_DISABLED_REASON
        return InstallSpecsResult(ok=False, blocked=True, reason=reason)

    # The same managed-install guard as in ensure(). A package-manager
    # install (Nix) has its venv in a read-only store, so the pip ladder
    # below can only burn 15s and then fail with EROFS. Report the remedy
    # for the deployment instead. A durable target overrides this guard,
    # as it does in ensure(): the NixOS container module sets
    # HERMES_MANAGED=true and a writable target, and the install works
    # there.
    if _lazy_install_target() is None:
        managed_by = _managed_system()
        if managed_by:
            return InstallSpecsResult(
                ok=False, blocked=True,
                reason="unsupported on a managed install: "
                + managed_install_reason("install_specs", None),
            )

    target = _lazy_install_target()
    display = "uv pip install " + (
        f"--target {target} " if target is not None else ""
    ) + " ".join(cleaned)

    logger.info("Installing pip specs %s (target=%s)", " ".join(cleaned), target or "venv")
    try:
        result = _venv_pip_install(cleaned, timeout=timeout)
    except Exception as exc:
        logger.warning("install_specs failed unexpectedly: %s", exc)
        return InstallSpecsResult(
            ok=False, command=display, stderr=f"install failed: {exc}"
        )

    # Freshly-installed dists must be visible to importers and metadata
    # checks in this same process (dashboard rechecks availability inline).
    try:
        import importlib
        importlib.invalidate_caches()
        import importlib.metadata as _md
        if hasattr(_md, "_cache_clear"):
            _md._cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass

    return InstallSpecsResult(
        ok=result.success,
        command=display,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def bundle_extras() -> list[str]:
    """The pyproject extras a bundled artifact stages for THIS host.

    THE authority the desktop payload staging asks. Every extra behind a
    :data:`LAZY_DEPS` feature ships in the bundle, minus the ones whose
    host-support probe refuses this host — so a bundled user never meets
    a first-use install for a backend the artifact could have carried.

    Callable as an authority because a bundled release builds natively,
    one runner per (os, arch): the host running this IS the target. The
    build lane therefore gets the same answer ``ensure`` would give a
    user of that artifact, which is what keeps the two from drifting.

    The two lanes now ask the probe the SAME question, and the answers
    still differ where they should. A gate refuses both. A wheel gap
    refuses neither here: the build lane compiles the sdist on the
    target runner and ships the result, and a runtime install on a user
    machine is the only place that forbids the compile.

    An extra that resolves to no specs is left out rather than passed to
    the exporter, which rejects an unknown extra name.
    """
    extras: list[str] = []
    for feature in sorted(LAZY_DEPS):
        try:
            check_supported(feature)
        except UnsupportedFeature:
            continue
        extra = feature_extra(feature)
        if extra not in extras and extra_specs(extra):
            extras.append(extra)
    return extras


def feature_report() -> list[tuple[str, str]]:
    """Every recorded feature paired with its state in THIS install.

    Diagnostics, for ``hermes doctor``. ``active_features`` answers the
    updater's question ("what should I refresh?") by dropping anything
    whose packages are absent; the dropped rows are exactly what a user
    needs to see, so this reports them instead.

    States:
      ``installed``   the packages are here and satisfy the current pins.
      ``stale``       here, but a pin has moved since — `hermes update`
                      refreshes it.
      ``missing``     recorded, packages absent from this install. The
                      user removed them by hand, or the overlay was
                      cleared (an ABI-stamp wipe after an interpreter
                      change) since the feature was last used.
      ``unsupported`` this host is gated off the feature.
      ``unknown``     a name no longer in LAZY_DEPS (a renamed or
                      removed backend).
    """
    report: list[tuple[str, str]] = []
    for feature in sorted(_read_feature_record()):
        if feature not in LAZY_DEPS:
            report.append((feature, "unknown"))
            continue
        try:
            check_supported(feature)
        except UnsupportedFeature:
            report.append((feature, "unsupported"))
            continue
        if not _feature_anchor_present(feature):
            report.append((feature, "missing"))
        elif feature_missing(feature):
            report.append((feature, "stale"))
        else:
            report.append((feature, "installed"))
    return report


def active_features() -> list[str]:
    """Return the list of features the user has lazy-installed and still has.

    The primary signal is the record file (:func:`_record_feature_use`).
    ``ensure`` writes a feature's name there on every call, and each backend
    calls ``ensure`` at start, so the record names exactly the features in
    use. A package check cannot do that: the extras share packages
    (sounddevice is in every audio extra), so presence of a package does not
    say which feature the user enabled.

    A recorded feature still needs its anchor package installed to count.
    The record says "used at some point"; a user who uninstalled the
    packages since then must not get them back on ``hermes update``.

    An install that predates the record has an empty one, so its first
    ``hermes update`` refreshes nothing. That is fine: ``ensure`` runs at
    each backend's start, repairs a stale pin there, and records the
    feature, so the next update refreshes it.

    Used by ``hermes update`` to figure out which lazy backends need a
    refresh pass when pins move in pyproject.toml.
    """
    recorded = _read_feature_record()
    return [
        f for f in LAZY_DEPS if f in recorded and _feature_anchor_present(f)
    ]


def _feature_anchor_present(feature: str) -> bool:
    """Is the anchor package of ``feature`` installed, at any version?"""
    anchor = _anchor_spec(feature_extra(feature))
    return anchor is not None and _is_present(anchor)


# The record of the features that ensure() has served. A JSON list, in
# the INSTALL's own state folder, beside the lazy-packages overlay whose
# contents it describes.
#
# Install-scoped because the packages are: a sealed install puts them in
# installs/<sha16>/lazy-packages, so two installs sharing one HERMES_HOME
# have separate package sets and a shared record would answer for the
# wrong one. Keeping both halves in one folder makes the record describe
# the overlay beside it, by construction.
_FEATURE_RECORD_NAME = "lazy-features.json"


def _feature_record_path() -> Path:
    from hermes_cli.boot_bootstrap import ensure_install_dir
    from installation.paths import get_install_root

    return ensure_install_dir(get_install_root()) / _FEATURE_RECORD_NAME


def _read_feature_record() -> set[str]:
    """Return the recorded feature names.

    A record that is absent or does not parse counts as empty, so a corrupt
    file heals on the next write instead of raising forever.
    """
    try:
        raw = json.loads(_feature_record_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(f) for f in raw}


def _write_feature_record(features: set[str]) -> None:
    """Write the record. A failure only costs the record, so never raise."""
    try:
        path = _feature_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(sorted(features), indent=0) + "\n", encoding="utf-8"
        )
    except OSError as e:
        logger.debug("Could not write the lazy-feature record: %s", e)


def _record_feature_use(feature: str) -> None:
    """Add ``feature`` to the record of features that ensure() has served."""
    recorded = _read_feature_record()
    if feature not in recorded:
        _write_feature_record(recorded | {feature})


def refresh_active_features(*, prompt: bool = False) -> dict[str, str]:
    """Re-run ``ensure`` for every feature the user has previously activated.

    Returns a ``{feature: status}`` map where status is one of:
        ``"current"``  — pins already satisfied, no install run
        ``"refreshed"`` — pins were stale, reinstall succeeded
        ``"failed: <reason>"`` — install attempt failed; caller decides
                                  whether to surface it (we don't raise)
        ``"skipped: <reason>"`` — gated off (config flag, user decline)

    Intended for ``hermes update``. Never raises; lazy-install failures
    here must not block the rest of the update flow.
    """
    return _refresh_features(active_features(), prompt=prompt, restoring=False)


def restore_features(features: list[str]) -> dict[str, str]:
    """Restore features captured before an explicit managed-runtime rebuild.

    Feature names are checked against :data:`LAZY_DEPS`, and installs remain
    subject to ``security.allow_lazy_installs``. An explicit opt-out therefore
    leaves the captured feature absent and reports it as skipped.
    """
    return _refresh_features(features, prompt=False, restoring=True)


def _refresh_features(
    features: list[str], *, prompt: bool, restoring: bool
) -> dict[str, str]:
    """Refresh or restore a known set of allowlisted lazy features."""
    results: dict[str, str] = {}
    for feature in features:
        if feature not in LAZY_DEPS:
            continue
        missing = feature_missing(feature)
        if not missing:
            results[feature] = "current"
            continue

        try:
            if restoring:
                ensure(feature, prompt=False)
                results[feature] = "restored"
            else:
                ensure(feature, prompt=prompt)
                results[feature] = "refreshed"
        except InstallSkipped as e:
            # The deployment or the user chose this outcome (platform
            # probe, managed/read-only install, config opt-out, prompt
            # decline) — the update command renders it as a non-error.
            results[feature] = f"skipped: {e.reason}"
        except FeatureUnavailable as e:
            results[feature] = f"failed: {e.reason}"
        except Exception as e:
            results[feature] = f"failed: {e}"
    return results


def ensure_and_bind(
    feature: str,
    importer: Callable[[], dict[str, Any]],
    target_globals: dict,
    *,
    prompt: bool = False,
) -> bool:
    """Ensure a feature is installed, then rebind names into the caller's globals.

    Combines :func:`ensure` with a post-install import step that rebinds
    module-level names.  This eliminates the error-prone pattern of manually
    listing every global that needs updating after lazy-install.

    ``importer`` is a zero-arg callable that returns a dict of
    ``{name: value}`` for all symbols the caller needs rebound.  It is called
    only after :func:`ensure` succeeds (or if the packages are already
    installed).

    Returns True on success, False if deps couldn't be installed or imported.

    Example usage in a platform adapter::

        def check_slack_requirements() -> bool:
            if SLACK_AVAILABLE:
                return True
            def _import():
                from slack_bolt.async_app import AsyncApp
                from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
                from slack_sdk.web.async_client import AsyncWebClient
                import aiohttp
                return {
                    "AsyncApp": AsyncApp,
                    "AsyncSocketModeHandler": AsyncSocketModeHandler,
                    "AsyncWebClient": AsyncWebClient,
                    "aiohttp": aiohttp,
                    "SLACK_AVAILABLE": True,
                }
            return ensure_and_bind("platform.slack", _import, globals(), prompt=False)
    """
    try:
        ensure(feature, prompt=prompt)
    except Exception:
        return False

    try:
        bindings = importer()
    except ImportError:
        return False

    target_globals.update(bindings)
    return True


def _main(argv: list[str]) -> int:
    """``python -m tools.lazy_deps <query>`` — the build-lane queries.

    The desktop payload staging runs these on the target runner, so the
    answers are that target's. A subprocess rather than an import
    because the caller is Node; one name per line so the caller needs no
    JSON parser.

    ``bundle-extras``       the extras to export for this target.
    """
    queries = {
        "bundle-extras": bundle_extras,
    }
    query = queries.get(argv[0]) if argv else None
    if query is None or len(argv) != 1:
        print(f"usage: python -m tools.lazy_deps {{{'|'.join(queries)}}}", file=sys.stderr)
        return 2
    for name in query():
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
