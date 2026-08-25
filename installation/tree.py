"""Derive who owns the running tree — no stored mode flags.

The install model is stamp-pure: ``.git`` says whether the tree is a
checkout, and the build stamp's REQUIRED ``updateMechanism`` says who
applies the next update.

* ``.git`` + stamp(``self``) is a **managed source checkout**: ``hermes
  update`` owns it. Installer-created checkouts are stamped at install
  time (and shipped pre-stamp installs are adopted once at a blessed
  root by ``step_adopt_blessed_checkout``).
* ``.git`` + no stamp is **somebody's working tree**: update refuses
  and points at ``git pull``.
* A stamp without ``.git`` is **sealed**: something external replaces
  the tree wholesale. The stamp names that steward in ``distribution``
  (``desktop-app``, ``docker``, ``nix``, or a future package manager).
* Neither is **unknown**.

If a future feature writes to user checkouts (nothing does today), it
must add an explicit opt-out fact FIRST. The old ``manageStyle: ejected``
stickiness guarded against desktop-side adoption and rematerialization;
both are deleted, so the guard went with them.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

BUILD_INFO_NAME = "install-stamp.json"

STEWARD_DESKTOP = "desktop-app"
STEWARD_DOCKER = "docker"
STEWARD_NIX = "nix"

# Who applies the next update. REQUIRED in every stamp — the writer
# (scripts/write_install_stamp.py) refuses to emit a stamp without it,
# and readers hard-fail a stamp missing it: a mechanism-less stamp means
# a build lane skipped the flag, and guessing here would misroute
# updates for every install of that artifact.
MECHANISM_SELF = "self"
MECHANISM_ELECTRON_UPDATER = "electron-updater"
MECHANISM_EXTERNAL = "external"
UPDATE_MECHANISMS = (MECHANISM_SELF, MECHANISM_ELECTRON_UPDATER, MECHANISM_EXTERNAL)

# What `hermes update` says in a sealed tree, per steward — THE single
# refusal table (config.format_docker_update_message and the update
# command's docker/nix/desktop-app branches all read it). The fallback
# covers stewards this build does not know (a newer package-manager value
# read by older code).
STEWARD_UPDATE_MESSAGES = {
    STEWARD_DESKTOP: (
        "✗ This Hermes runs from inside the desktop app bundle.\n"
        "\n"
        "Manage updates from within the desktop app.\n"
        "Prefer a self-managed source install? See:\n"
        "  https://hermes-agent.nousresearch.com/docs/user-guide/switching-to-source"
    ),
    STEWARD_DOCKER: """\
✗ ``hermes update`` doesn't apply inside the Docker container.

Hermes Agent runs as a published image (nousresearch/hermes-agent), not a
git checkout — the container has no working tree to pull into.  Update by
pulling a fresh image and restarting your container instead:

  docker pull nousresearch/hermes-agent:latest
  # then restart whatever started the container, e.g.:
  docker compose up -d --force-recreate hermes-agent
  # or, for ad-hoc runs, exit the current container and `docker run` again

Verify the new version after restart:
  docker run --rm nousresearch/hermes-agent:latest --version

Notes:
  • If you pinned a specific tag (e.g. ``:v0.14.0``) the ``:latest`` tag
    won't move your container — pull the newer tag you actually want, or
    switch to ``:latest`` / ``:main`` for rolling updates.  See available
    tags at https://hub.docker.com/r/nousresearch/hermes-agent/tags
  • Your config and session history live under ``$HERMES_HOME`` (``/opt/data``
    in the container, typically bind-mounted from the host) and persist
    across image upgrades — re-pulling doesn't lose any state.
  • Running a fork?  Build your own image with this repo's ``Dockerfile``
    and replace the ``docker pull`` step with your build/push pipeline.""",
    STEWARD_NIX: (
        "✗ This Hermes runs from the Nix store.\n"
        "\n"
        "The store path is immutable. Update through your flake:\n"
        "  nix flake update && rebuild your profile or system"
    ),
}

_STEWARD_FALLBACK_MESSAGE = (
    "✗ This Hermes install is managed by {steward}.\n"
    "\n"
    "The tree has no git checkout, so `hermes update` cannot update it.\n"
    "Update it with the tool that installed it."
)

# What the uninstaller says when it refuses to remove code from a sealed
# tree. The steward put the code there; the steward removes it. The
# desktop-app message is per-OS because each OS owns app removal
# differently.
_STEWARD_DELETE_DATA_PREAMBLE = "To delete your Hermes data (chats, configuration, etc),\n"
_STEWARD_DELETE_DATA_CLI = "run:\n$ hermes uninstall --data\n"
_STEWARD_DELETE_DATA_DESKTOP = "Open Hermes Desktop, go to Settings -> About, and delete your data from there.\n"

_STEWARD_UNINSTALL_MESSAGES = {
    STEWARD_DOCKER: (
        "✗ This Hermes runs from a Docker image.\n"
        "\n"
        "There is no code to uninstall — remove the container and image:\n"
        "  docker rm <container> && docker rmi nousresearch/hermes-agent\n"
        "\n" +
        _STEWARD_DELETE_DATA_PREAMBLE +
        _STEWARD_DELETE_DATA_CLI
    ),
    STEWARD_NIX: (
        "✗ This Hermes was installed by Nix.\n"
        "\n"
        "The store path is immutable — uninstall it the same way you\n"
        "installed it: remove hermes-agent from your flake / profile\n"
        "(e.g. `nix profile remove`), then rebuild.\n"
        "\n" +
        _STEWARD_DELETE_DATA_PREAMBLE +
        _STEWARD_DELETE_DATA_CLI
    ),
}

_STEWARD_MANAGED_BY_DESKTOP = "✗ Hermes is managed by the desktop app.\n"

_STEWARD_DESKTOP_UNINSTALL_BY_PLATFORM = {
    "win32": (
        _STEWARD_MANAGED_BY_DESKTOP +
        "\n"
        "Remove the app from Windows Settings → Apps → Installed apps.\n" +
        _STEWARD_DELETE_DATA_PREAMBLE +
        _STEWARD_DELETE_DATA_DESKTOP
    ),
    "darwin": (
        _STEWARD_MANAGED_BY_DESKTOP +
        "\n"
        "Quit the app and drag Hermes.app from Applications to the Trash.\n" +
        _STEWARD_DELETE_DATA_PREAMBLE +
        _STEWARD_DELETE_DATA_DESKTOP
    ),
}

_STEWARD_DESKTOP_UNINSTALL_DEFAULT = (
    _STEWARD_MANAGED_BY_DESKTOP +
    "\n"
    "Delete the Hermes AppImage (or app directory) from wherever you\n"
    "saved it.\n" +
    _STEWARD_DELETE_DATA_PREAMBLE +
    _STEWARD_DELETE_DATA_DESKTOP
)

_STEWARD_UNINSTALL_FALLBACK = (
    "✗ Hermes is managed by {steward}.\n"
    "\n"
    "The tree has no git checkout, so the uninstaller will not remove it.\n"
    "Remove it with the tool that installed it.\n"
    "\n" +
    # A generic package-manager steward has no desktop app, and this
    # refusal prints in a CLI context — point at the CLI data path.
    _STEWARD_DELETE_DATA_PREAMBLE +
    _STEWARD_DELETE_DATA_CLI
)


def steward_uninstall_message(steward: str, platform: "str | None" = None) -> str:
    """The uninstall refusal text for a sealed tree."""
    if steward == STEWARD_DESKTOP:
        key = platform if platform is not None else sys.platform
        return _STEWARD_DESKTOP_UNINSTALL_BY_PLATFORM.get(key, _STEWARD_DESKTOP_UNINSTALL_DEFAULT)
    message = _STEWARD_UNINSTALL_MESSAGES.get(steward)
    if message is not None:
        return message
    return _STEWARD_UNINSTALL_FALLBACK.format(steward=steward)


@dataclass(frozen=True)
class GitCheckout:
    """A tree with .git — `hermes update` owns it."""

    root: Path


@dataclass(frozen=True)
class Sealed:
    """A gitless tree — the steward replaces it wholesale."""

    root: Path
    steward: str


def read_build_info(project_root: Path) -> dict:
    """The baked build stamp of ``project_root``, or ``{}``.

    Raises ``RuntimeError`` on a ``payload: light`` stamp: a light artifact
    ships no Python runtime, so a Python process reading its own stamp as
    light means the artifact was mispackaged. Failing loudly here beats
    every consumer misclassifying the tree.

    Also raises ``RuntimeError`` on a stamp without ``updateMechanism``:
    the field is required, and a stamp missing it means the writing build
    lane (scripts/write_install_stamp.py caller) must be fixed. Guessing a
    mechanism would misroute updates for every install of that artifact.
    """
    try:
        data = json.loads((Path(project_root) / BUILD_INFO_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("payload") == "light":
        raise RuntimeError(
            f"install-stamp.json at {project_root} marks this artifact as 'light' "
            "(no agent runtime). No Python process can legitimately run from a "
            "light artifact — this build is mispackaged."
        )
    if data.get("updateMechanism") not in UPDATE_MECHANISMS:
        raise RuntimeError(
            f"install-stamp.json at {project_root} is missing a valid "
            f"'updateMechanism' (one of {', '.join(UPDATE_MECHANISMS)}). The build "
            "lane that wrote this stamp must pass --update-mechanism to "
            "scripts/write_install_stamp.py (or bake the field directly)."
        )
    return data


def is_bundled_payload(project_root: Path) -> bool:
    """True when ``project_root`` is a bundled desktop payload's ``repo/``.

    THE shape predicate for the sealed-bundle branch, and a mirror of the
    Electron shell's ``installShape() === 'bundled'``: the build stamp is
    the authority, so filesystem coincidences (a sibling ``bin/``, a
    surviving ``apps/desktop/package.json``) can never promote a checkout
    into it. A missing, foreign, or unreadable stamp means NOT bundled.
    """
    try:
        return read_build_info(project_root).get("payload") == "bundled"
    except Exception:  # noqa: BLE001 — a bad stamp must not take a caller down
        return False


def runtime_tree(project_root: Path) -> GitCheckout | Sealed:
    """Classify the tree at ``project_root``.

    ``.git`` present (a directory, or a worktree/submodule gitfile) means a
    git checkout. Everything else is sealed, with the steward read from the
    build stamp; a missing or unknown stamp gives steward ``"unknown"``.
    """
    root = Path(project_root)
    if (root / ".git").exists():
        return GitCheckout(root=root)

    distribution = read_build_info(root).get("distribution")
    steward = distribution if isinstance(distribution, str) and distribution else "unknown"
    return Sealed(root=root, steward=steward)


def steward_update_message(steward: str) -> str:
    """The `hermes update` refusal text for a sealed tree."""
    message = STEWARD_UPDATE_MESSAGES.get(steward)
    if message is not None:
        return message
    return _STEWARD_FALLBACK_MESSAGE.format(steward=steward)


# Stewards install_method() reports as-is. An unknown steward value (a newer
# package manager read by older code) reports "unknown" so consumers do not
# branch on an enum member they never heard of.
_KNOWN_STEWARDS = frozenset({STEWARD_DESKTOP, STEWARD_DOCKER, STEWARD_NIX})


def install_method(project_root: Path) -> str:
    """Derive the install method of the tree at ``project_root``.

    Stamp-pure: the ladder reads the tree itself — ``.git`` plus the
    stamp's ``updateMechanism`` — and never path-matches install roots.
    (The blessed-root table survives in exactly one place: the one-time
    adoption step ``step_adopt_blessed_checkout``, which stamps shipped
    pre-stamp checkouts at those roots. It is a birth certificate, not a
    classification rung.)

    * ``.git`` + stamp with ``updateMechanism: self`` → ``git``
      (managed source checkout; `hermes update` owns it)
    * ``.git`` + no stamp → ``source`` (somebody's working tree;
      update refuses and points at ``git pull``)
    * stamp + no ``.git`` → sealed: the stamp's ``distribution`` names
      the steward (``docker``, ``nix``, ``desktop-app``)
    * neither → ``unknown``

    A ``.git`` tree whose stamp names a non-``self`` mechanism is NOT
    managed by `hermes update` — it classifies as ``source`` (the stamp
    was baked for an artifact, not for this checkout).
    """
    tree = runtime_tree(project_root)
    if isinstance(tree, Sealed):
        return tree.steward if tree.steward in _KNOWN_STEWARDS else "unknown"
    info = read_build_info(tree.root)
    if info and info.get("updateMechanism") == MECHANISM_SELF:
        return "git"
    return "source"
