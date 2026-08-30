"""Image-managed install refusal contract (#91277 Phase 3).

One shared admission gate for every surface that can start an in-place
``hermes update`` mutation (CLI apply, CLI --check, dashboard update
endpoint). The decision layers:

1. **Baked provenance marker** (``/etc/hermes/image-provenance.json``,
   written by the image build — see :mod:`hermes_cli.image_provenance`):
   authoritative ground truth that this filesystem came from an immutable
   image. Fail-closed: a present-but-malformed marker still refuses.
2. **Install stamp** (``install-stamp.json`` beside the running code, read
   via :mod:`hermes_cli.steward`): a sealed tree (no ``.git``) names its
   steward in ``distribution`` — ``desktop-app``, ``docker``, ``nix``. The
   steward replaces the tree wholesale, so in-place update refuses with
   the steward's own instructions (restack's steward-refusal ladder).
3. **Filesystem heuristics** (``detect_install_method()``): the pre-existing
   docker/nix/apt detection, kept as the fallback for images built before
   the marker existed and for package-managed installs that have no image
   marker at all.

A refusal prints the real update command for the deployment kind, records a
``refused`` receipt (so fleet tooling sees "this install cannot self-update,
use <command>" instead of a silent non-update), and exits 2 on CLI surfaces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpdateRefusal:
    """Why an in-place update is refused, and what to run instead."""

    code: str              # image-marker | image-marker-invalid | docker | nix | apt | desktop-app | <steward>
    message: str           # full user-facing text (multi-line ok)
    update_command: str    # the one-line remediation command


def evaluate_update_admission(project_root: Path) -> Optional[UpdateRefusal]:
    """Return an :class:`UpdateRefusal` when in-place update must not run.

    ``None`` means the install is eligible for in-place update (git checkout
    or unknown-but-mutable). Never raises; on any internal error it falls
    back to the heuristic layer only.
    """
    # Layer 1: baked provenance marker — authoritative when present.
    try:
        from hermes_cli.image_provenance import read_image_provenance

        provenance = read_image_provenance()
        if provenance is not None:
            from hermes_cli.config import (
                format_docker_update_message,
                recommended_update_command_for_method,
            )

            if not provenance.valid:
                # Present but malformed: still image-managed — an integrity
                # defect is never permission to mutate the image in place.
                command = recommended_update_command_for_method("docker")
                return UpdateRefusal(
                    code="image-marker-invalid",
                    message=(
                        "✗ This install is image-managed, but its provenance "
                        f"marker is invalid ({provenance.error}).\n"
                        "  In-place update is disabled. Update by pulling a "
                        f"new image:\n    {command}"
                    ),
                    update_command=command,
                )
            manager = provenance.manager
            if manager == "docker":
                return UpdateRefusal(
                    code="image-marker",
                    message=format_docker_update_message(),
                    update_command=recommended_update_command_for_method("docker"),
                )
            command = recommended_update_command_for_method(manager)
            return UpdateRefusal(
                code="image-marker",
                message=command,
                update_command=command,
            )
    except Exception as exc:
        logger.debug("Image provenance check failed (using heuristics): %s", exc)

    # Layer 2: install stamp / steward classification. A sealed tree (no
    # ``.git``) belongs to a steward — the desktop app bundle, a Docker
    # image, the Nix store — and only the steward updates it. This is the
    # rung that covers ``desktop-app``, which the heuristics below never
    # detect (the payload has no .install_method stamp and no .git).
    try:
        from hermes_cli.steward import (
            STEWARD_DESKTOP,
            STEWARD_DOCKER,
            STEWARD_NIX,
            sealed_steward,
            steward_update_message,
        )

        steward = sealed_steward(project_root)
        if steward is not None and steward != "unknown":
            from hermes_cli.config import recommended_update_command_for_method

            if steward == STEWARD_DOCKER:
                from hermes_cli.config import format_docker_update_message

                return UpdateRefusal(
                    code="docker",
                    message=format_docker_update_message(),
                    update_command=recommended_update_command_for_method("docker"),
                )
            if steward == STEWARD_NIX:
                return UpdateRefusal(
                    code="nix",
                    message=steward_update_message(steward),
                    update_command=recommended_update_command_for_method("nix"),
                )
            # desktop-app and future package managers: there is no CLI
            # remediation command — the steward's own instructions ARE the
            # remediation (recommended_update_command_for_method would
            # falsely answer "hermes update" for methods it doesn't know).
            command = (
                "Manage updates from within the desktop app"
                if steward == STEWARD_DESKTOP
                else f"update via {steward}"
            )
            return UpdateRefusal(
                code=steward,
                message=steward_update_message(steward),
                update_command=command,
            )
    except Exception as exc:
        logger.debug("Steward admission check failed: %s", exc)

    # Layer 3: pre-existing filesystem heuristics, verbatim semantics.
    try:
        from hermes_cli.config import (
            detect_install_method,
            format_docker_update_message,
            is_nix_install_method,
            recommended_update_command_for_method,
        )

        method = detect_install_method(project_root)
        if method == "docker":
            return UpdateRefusal(
                code="docker",
                message=format_docker_update_message(),
                update_command=recommended_update_command_for_method("docker"),
            )
        if is_nix_install_method(method):
            command = recommended_update_command_for_method(method)
            return UpdateRefusal(
                code="nix",
                message=command,
                update_command=command,
            )
    except Exception as exc:
        logger.debug("Install-method admission check failed: %s", exc)
    return None


def record_refusal_receipt(refusal: UpdateRefusal) -> None:
    """Write a minimal ``refused`` receipt for a blocked update attempt.

    Gives fleet tooling a durable record that an update was ATTEMPTED and
    refused ("not updatable in place, use <command>") instead of a silent
    nothing. Best-effort; never raises.
    """
    try:
        from hermes_cli.update_receipt import (
            begin_update_receipt,
            finalize_update_receipt,
            record_step,
        )

        begin_update_receipt()
        record_step(
            "admission",
            False,
            f"not updatable in place ({refusal.code}); use: {refusal.update_command}",
        )
        finalize_update_receipt("refused", stop_reason=refusal.code)
    except Exception as exc:
        logger.debug("Could not record refusal receipt: %s", exc)
