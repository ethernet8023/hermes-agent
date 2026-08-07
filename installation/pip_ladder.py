"""The ONE uv → pip → ensurepip install ladder.

Three copies of this strategy grew independently — dep-inventory items
#26 (``hermes_cli.tools_config._pip_install``), #27
(``tools.lazy_deps._venv_pip_install``, whose docstring admitted being a
mirror of #26), and #32 (``agent/lsp/install.py``'s pip-target branch).
Each drifted its own policy decisions into the mechanics. This module
owns the mechanics; callers own the policy through arguments:

* ``uv_bin`` — the caller decides HOW MUCH acquiring uv is worth. A
  post-setup hook passes ``ensure_uv()`` (downloading uv is in scope
  during setup); a mid-turn lazy install passes ``resolve_uv()`` (a
  download as a side effect of an optional import is not); None skips
  straight to pip.
* ``uv_resolver_failure_is_final`` — lazy installs treat a uv RESOLVER
  failure as authoritative (falling to pip would discard uv policy like
  ``exclude-newer`` and could install a quarantined release); setup
  hooks prefer any-tier-that-works.
* ``target``/``constraints`` — the durable ``--target`` mode sealed
  installs use for the lazy overlay.

Stdlib-only by the run-don't-parse audit: the ladder exists precisely
for moments when the venv is missing pip, so it must not need anything
a broken venv cannot supply.

Tier order and why:

1. ``uv pip install`` — fast, and does not need pip in the venv. The
   Windows installer creates the venv with ``uv venv``, which seeds NO
   pip, so a pip-first ladder failed on every fresh Windows install.
2. ``python -m pip install`` — stdlib venvs.
3. ``python -m ensurepip --upgrade`` then retry pip — heals the
   ``uv venv`` no-pip case when uv itself is unavailable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


@dataclass(frozen=True)
class LadderResult:
    ok: bool
    stdout: str
    stderr: str
    tier: str  # "uv" | "pip" | "none"

    @property
    def returncode(self) -> int:
        # CompletedProcess-shaped for callers migrating off subprocess.
        return 0 if self.ok else 1


def default_uv() -> Optional[str]:
    """The managed uv from the store facts, else None. Pure lookup.

    ``registry.tool_path`` is the same resolution every other managed
    tool uses (decision 4's direction: no more parallel uv-path logic).
    """
    try:
        from installation.registry import tool_path

        found = tool_path("uv")
    except Exception:  # noqa: BLE001 — a lookup must never sink the ladder
        return None
    if found is not None and os.access(found, os.X_OK):
        return str(found)
    return None


def pip_install(
    specs: Sequence[str],
    *,
    uv_bin: Optional[str] = None,
    timeout: int = 300,
    target: Optional[Path] = None,
    constraints: Optional[Path] = None,
    overrides: Optional[Path] = None,
    env: Optional[dict] = None,
    capture_output: bool = True,
    creationflags: int = 0,
    uv_resolver_failure_is_final: bool = False,
) -> LadderResult:
    """Install *specs* into the running interpreter's venv (or *target*).

    Never raises: every failure comes back as a ``LadderResult`` whose
    stderr says which tier failed and why.

    *overrides* is a requirements-style file of security floors. The uv
    tier passes it as ``--overrides`` (unconditional pins that beat the
    backend spec's own caps). pip has no such flag — the CALLER re-asserts
    the floor after a pip-tier install with a ``--no-deps`` repair pass
    (see tools/lazy_deps._pip_reassert_overrides). Handing pip the floor
    as a constraint instead would hold the pinned package but resolve the
    backend backwards (measured on the DingTalk case: alibabacloud-tea-
    openapi 0.4.5 → 0.3.16, a two-year-old sdist). ``LadderResult.tier``
    tells the caller which tier ran.
    """
    if not specs:
        return LadderResult(True, "", "", "none")

    extra: list[str] = []
    if target is not None:
        extra += ["--target", str(target)]
    if constraints is not None:
        extra += ["--constraint", str(constraints)]
    uv_extra: list[str] = list(extra)
    pip_extra: list[str] = list(extra)
    if overrides is not None:
        uv_extra += ["--overrides", str(overrides)]

    run_kwargs: dict = {
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "stdin": subprocess.DEVNULL,
        "creationflags": creationflags,
    }
    if capture_output:
        run_kwargs["capture_output"] = True

    # ── tier 1: uv ─────────────────────────────────────────────────────
    if uv_bin is None:
        uv_bin = default_uv()
    if uv_bin:
        venv_root = Path(sys.executable).parent.parent
        uv_env = dict(env if env is not None else os.environ)
        # uv installs into VIRTUAL_ENV, not the interpreter that spawned
        # it; without this a caller's stale VIRTUAL_ENV wins.
        uv_env["VIRTUAL_ENV"] = str(venv_root)
        try:
            result = subprocess.run(
                [str(uv_bin), "pip", "install", *uv_extra, *specs],
                timeout=timeout,
                env=uv_env,
                **run_kwargs,
            )
            if result.returncode == 0:
                return LadderResult(True, result.stdout or "", result.stderr or "", "uv")
            if uv_resolver_failure_is_final:
                # uv SAW the requirements and said no. pip would resolve
                # them again without uv's policy (exclude-newer et al.).
                return LadderResult(
                    False, result.stdout or "", result.stderr or "", "uv"
                )
            # Otherwise fall through: uv may have failed for a reason pip
            # handles (network shape, index quirk).
        except subprocess.TimeoutExpired as exc:
            if uv_resolver_failure_is_final:
                return LadderResult(False, "", f"uv pip install timed out: {exc}", "uv")
        except FileNotFoundError:
            # Resolved path vanished between lookup and spawn — uv never
            # evaluated anything, so pip is a valid fallback everywhere.
            pass
        except OSError as exc:
            # Same class as vanished: uv could not even start (noexec
            # mount, permission bit lost). Never a resolver verdict.
            if uv_resolver_failure_is_final:
                return LadderResult(False, "", f"uv could not run: {exc}", "uv")

    # ── tier 2: pip, with the ensurepip bootstrap ──────────────────────
    pip_cmd = [sys.executable, "-m", "pip"]
    try:
        probe = subprocess.run(
            pip_cmd + ["--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        if probe.returncode != 0:
            raise FileNotFoundError("pip not in venv")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        try:
            subprocess.run(
                [sys.executable, "-m", "ensurepip", "--upgrade", "--default-pip"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=True,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except Exception as exc:  # noqa: BLE001 — the contract is never-raise
            return LadderResult(
                False, "", f"pip not available and ensurepip failed: {exc}", "none"
            )

    try:
        result = subprocess.run(
            pip_cmd + ["install", *pip_extra, *specs],
            timeout=timeout,
            env=env,
            **run_kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        return LadderResult(False, "", f"pip install timed out: {exc}", "pip")
    except Exception as exc:  # noqa: BLE001 — the contract is never-raise
        return LadderResult(False, "", f"pip install failed: {exc}", "pip")
    return LadderResult(
        result.returncode == 0, result.stdout or "", result.stderr or "", "pip"
    )
