"""The ONE browser locator: the agent-browser driver, and its engine.

Browsing needs two different things, and the code that used to answer
for it asked one question about both. ``_DEP_CHECKS["browser"]`` read

    _agent_browser_resolves() or _has_system_browser()

so a machine with Chrome on PATH and no driver answered True, the lazy
installer skipped the provision it existed to run, and the caller then
raised "agent-browser CLI not found". Having a browser installed
SUPPRESSED the browser install. The two questions are separate here:

* :func:`driver_path` — the agent-browser CLI. Pinned, so an install
  can stage it on demand. This is the thing a caller executes.
* :func:`engine_path` — the Chromium the driver drives. Pinned too, at
  a revision the driver is known to work with.

Both are ``optional: true`` in the pin table, so both return
``Optional[Path]`` and None is a normal answer, not an error state —
the :mod:`installation.git` posture. Pair either with
:func:`browser_install_guidance` when reporting to a user.

**No system-browser rung.** A machine Chrome is whatever version the
machine happens to carry, against a driver pinned to one revision, and
the pair is what the pin table exists to fix. The one honoured override
is an explicit ``AGENT_BROWSER_EXECUTABLE_PATH``: the Docker image
resolves its baked-in Chromium into that variable at boot
(``docker/stage2-hook.sh``), and a user who sets it has said which
browser they mean.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

__all__ = [
    "DRIVER_TOOL",
    "ENGINE_TOOLS",
    "browser_install_guidance",
    "driver_path",
    "engine_path",
    "provision_driver",
]

#: The pin table's name for the agent-browser CLI.
DRIVER_TOOL = "agent-browser"

#: The engines the driver launches, in preference order. The pin table
#: records both as ``agent-browser``'s ``requires``, so provisioning the
#: driver stages the pair with it — a driver without an engine is not a
#: state this module may produce.
ENGINE_TOOLS = ("chromium", "chromium-headless-shell")

#: The documented way to point the driver at a specific browser binary.
#: agent-browser reads it directly, so an override here needs no further
#: plumbing from us.
ENGINE_OVERRIDE_ENV = "AGENT_BROWSER_EXECUTABLE_PATH"


def _managed(tool: str) -> Optional[Path]:
    """The provisioned binary for *tool*, or None.

    Fail-open like every registry consult: a broken runtime dir reports
    "not provisioned" rather than taking a caller down.
    """
    try:
        from installation.env import managed_tool_binary

        return managed_tool_binary(tool)
    except Exception:  # noqa: BLE001 — a lookup must not take a caller down
        return None


def driver_path() -> Optional[Path]:
    """The pinned agent-browser CLI, or None when it is not staged.

    A pure lookup. The driver is a native binary, so this answer needs
    no Node, and it is the rung a sealed bundle can answer from with no
    network and nothing on PATH.
    """
    return _managed(DRIVER_TOOL)


def provision_driver() -> bool:
    """Stage the pinned driver (and its engine). True when it landed.

    :func:`installation.provisioner.provision_tool` downloads the pinned
    artifact, verifies its digest before extraction, walks the
    ``requires`` closure so the engine arrives with it, and records the
    result. A second call is a no-op, and ``hermes update``'s sweep
    keeps it at the pin from then on.

    Ungated, like ``provision_tool`` itself: whether an install is
    allowed to happen right now is policy, and it belongs to the caller
    that knows why it is asking. A lazy runtime path owes the user the
    ``security.allow_lazy_installs`` check first; a user who typed
    ``--setup`` has already answered it.
    """
    try:
        from installation.provisioner import provision_tool

        return provision_tool(DRIVER_TOOL).provisioned
    except Exception:  # noqa: BLE001 — a failed download is a normal outcome
        return False


def engine_path() -> Optional[Path]:
    """The Chromium the driver should launch, or None.

    Resolution order:

    1. ``AGENT_BROWSER_EXECUTABLE_PATH`` — the explicit override. Docker
       sets it to the image's own Chromium; a user who sets it has named
       the browser they mean.
    2. The pinned chromium, then the pinned headless shell.
    3. None.

    A Chrome that merely happens to be on PATH is NOT a rung: it is an
    unpinned version against a pinned driver.
    """
    override = os.environ.get(ENGINE_OVERRIDE_ENV, "").strip()
    if override:
        if os.path.isfile(override):
            return Path(override)
        resolved = shutil.which(override)
        if resolved:
            return Path(resolved)

    for tool in ENGINE_TOOLS:
        found = _managed(tool)
        if found is not None:
            return found
    return None


def browser_install_guidance() -> str:
    """One line telling the user how to get the browser stack."""
    return (
        "Run `hermes update` to stage the pinned agent-browser and Chromium, "
        f"or set {ENGINE_OVERRIDE_ENV} to a Chrome or Chromium binary you "
        "already have."
    )
