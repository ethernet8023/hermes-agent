"""The ONE managed-uv install path.

Three copies of an install strategy grew independently — dep-inventory
items #26 (``hermes_cli.tools_config._pip_install``), #27
(``tools.lazy_deps._venv_pip_install``, whose docstring admitted being a
mirror of #26), and #32 (``agent/lsp/install.py``'s pip-target branch).
Each drifted its own policy decisions into the mechanics. This module
owns the mechanics; callers own the policy through arguments.

There is no pip tier. Every install goes through the managed uv that
``installation.registry`` resolves from the store facts. The reasons:

* A pip tier resolves the same requirements a second time WITHOUT uv
  policy. ``exclude-newer`` and the ``[tool.uv]`` overrides live in uv
  configuration only, so a pip fallback can install a release the
  project quarantined, or resolve a backend backwards. The lazy path
  already treated a uv resolver verdict as final for exactly this
  reason, and the setup-hook path did not. One of the two was wrong.
* pip has no ``--overrides``. The pip tier needed a follow-up
  ``--no-deps`` repair pass to re-assert the security floor, which is a
  second resolution against a tree pip already changed.
* The venv is not guaranteed to have pip at all. The Windows installer
  creates it with ``uv venv``, which seeds none, so the pip tier needed
  an ensurepip bootstrap to heal a case that only existed because the
  ladder insisted on reaching pip.

An unprovisioned tree is a provisioning fault, not an install fault.
The result then names ``python -m installation.provisioner`` instead of
silently taking a weaker path.

``agent/_early_recovery.py`` keeps its own ensurepip call. That is
disaster recovery for a venv that cannot import this module, and it is
a documented exemption from this rule.

Stdlib-only by the run-don't-parse audit: this module runs in moments
when the venv is damaged, so it must not need anything a broken venv
cannot supply.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

UNPROVISIONED_MESSAGE = (
    "no managed uv found: this tree is not provisioned. "
    "Run: python -m installation.provisioner"
)


@dataclass(frozen=True)
class LadderResult:
    ok: bool
    stdout: str
    stderr: str
    tier: str  # "uv" | "none"

    @property
    def returncode(self) -> int:
        # CompletedProcess-shaped for callers migrating off subprocess.
        return 0 if self.ok else 1


def default_uv() -> Optional[str]:
    """The managed uv from the store facts, else None. Pure lookup.

    ``registry.tool_path`` is the same resolution every other managed
    tool uses (decision 4: no parallel uv-path logic).
    """
    try:
        from installation.registry import tool_path

        found = tool_path("uv")
    except Exception:  # noqa: BLE001 — a lookup must never sink the install
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
    no_build: bool = False,
) -> LadderResult:
    """Install *specs* into the running interpreter's venv (or *target*).

    Never raises: every failure comes back as a ``LadderResult`` whose
    stderr says what failed and why.

    *uv_bin* names the uv to use. Callers that have already resolved one
    pass it; the default is the managed uv from the store facts. When
    there is no managed uv the result is a failure that names the
    provisioner, because an install without uv policy is worse than no
    install.

    *overrides* is a requirements-style file of security floors, passed
    to uv as ``--overrides`` (unconditional pins that beat the backend
    spec's own caps).

    *no_build* forbids building any package from an sdist. Set it for an
    install onto a user machine, which cannot be assumed to have a
    compiler toolchain: without it a package that publishes no wheel for
    the host compiles, and the user meets a compiler error instead of a
    clear refusal. uv reports the refusal with a hint naming the package
    (see ``wheel_gap`` below).
    """
    if not specs:
        return LadderResult(True, "", "", "none")

    if uv_bin is None:
        uv_bin = default_uv()
    if not uv_bin:
        return LadderResult(False, "", UNPROVISIONED_MESSAGE, "none")

    args: list[str] = []
    if target is not None:
        args += ["--target", str(target)]
    if constraints is not None:
        args += ["--constraint", str(constraints)]
    if overrides is not None:
        args += ["--overrides", str(overrides)]
    if no_build:
        args.append("--no-build")

    run_kwargs: dict = {
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "stdin": subprocess.DEVNULL,
        "creationflags": creationflags,
    }
    if capture_output:
        run_kwargs["capture_output"] = True

    venv_root = Path(sys.executable).parent.parent
    uv_env = dict(env if env is not None else os.environ)
    # uv installs into VIRTUAL_ENV, not the interpreter that spawned it;
    # without this a caller's stale VIRTUAL_ENV wins.
    uv_env["VIRTUAL_ENV"] = str(venv_root)

    try:
        result = subprocess.run(
            [str(uv_bin), "pip", "install", *args, *specs],
            timeout=timeout,
            env=uv_env,
            **run_kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        return LadderResult(False, "", f"uv pip install timed out: {exc}", "uv")
    except FileNotFoundError:
        # The resolved path vanished between the lookup and the spawn.
        return LadderResult(False, "", f"managed uv missing at {uv_bin}", "none")
    except OSError as exc:
        # uv could not start (noexec mount, permission bit lost).
        return LadderResult(False, "", f"uv could not run: {exc}", "none")

    return LadderResult(
        result.returncode == 0, result.stdout or "", result.stderr or "", "uv"
    )


# uv's own words when --no-build refuses a package that ships no usable
# wheel. It names the package in backticks, which is the whole reason
# this is detectable rather than guessed:
#
#   hint: Wheels are required for `pywinpty` because building from source
#   is disabled for all packages (i.e., with `--no-build`)
#
# Matching the hint and not the generic "No solution found" header is
# deliberate. uv emits that header for every unsatisfiable resolution —
# a version that does not exist, a conflict, a disabled network — and
# only this hint means "a wheel is missing for THIS host". Its sibling
# hints stay distinguishable: a cold-cache offline run says "Packages
# were unavailable because the network was disabled", and a bad version
# emits no hint at all.
_WHEEL_GAP_HINT = re.compile(
    r"Wheels are required for `(?P<package>[^`]+)`.*?because building from source is disabled",
    re.IGNORECASE | re.DOTALL,
)


def wheel_gap(result: LadderResult) -> Optional[str]:
    """The package a ``no_build`` install refused, or None.

    None for success and for every other failure, so a caller can turn
    this one cause into its own error and let the rest stay a generic
    install failure.
    """
    if result.ok:
        return None
    m = _WHEEL_GAP_HINT.search(f"{result.stderr}\n{result.stdout}")
    return m.group("package") if m else None
