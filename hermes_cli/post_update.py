"""Post-update maintenance steps shared by ``hermes update`` and boot bootstrap.

Each step operates on user state (config.yaml, skills, state.db) or machine
state (cua-driver), never on the install tree. Every step is idempotent and
self-gating: running it twice, or from two installs that share one
HERMES_HOME, converges. The caller (boot_bootstrap, update_cmd) decides WHEN
steps run; this module owns WHAT they do.

Steps declare a scope:

* ``home``    — mutates the active HERMES_HOME (per profile).
* ``machine`` — machine-global state shared by every profile.

The scopes must match the record that gates them in ``boot_bootstrap``
(home record vs machine record). See
.hermes/plans/2026-08-10_163500-boot-time-post-update-bootstrap.md.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# config migration (backup / migrate / verify / restore)
# ---------------------------------------------------------------------------

def _backup_path(path: Path, stamp: str) -> Path:
    base = path.with_name(f"{path.name}.bak-{stamp}")
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}.bak-{stamp}.{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not choose a backup path for {path}")


def _backup_existing(paths: Iterable[Path]) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups: dict = {}
    for path in paths:
        if not path.is_file():
            continue
        dest = _backup_path(path, stamp)
        shutil.copy2(path, dest)
        backups[path] = dest
    return backups


def _restore_backups(backups: dict) -> list:
    restored = []
    for original, backup in backups.items():
        if not backup.is_file():
            continue
        shutil.copy2(backup, original)
        restored.append(original)
    return restored


def step_migrate_config() -> dict:
    """Migrate config.yaml to the current schema, non-interactively.

    Same shape as scripts/docker_config_migrate.py: back up config + .env,
    migrate, verify the version advanced, restore the backups on failure.
    No-op when the on-disk version is current (the 99% case).
    """
    from hermes_cli.config import (
        check_config_version,
        get_config_path,
        get_env_path,
        migrate_config,
    )
    from hermes_cli.config_migrations import (
        SUPPORT_FLOOR_VERSION,
        support_floor_message,
    )

    current_ver, latest_ver = check_config_version()
    if current_ver >= latest_ver:
        return {"ok": True, "skipped": "up-to-date"}
    if current_ver < SUPPORT_FLOOR_VERSION:
        # migrate_config() refuses sub-floor configs and leaves the file
        # untouched; warn instead of failing the boot.
        logger.warning("config migration skipped: %s", support_floor_message())
        return {"ok": True, "skipped": "below-support-floor"}

    backups = _backup_existing((get_config_path(), get_env_path()))
    try:
        migrate_config(interactive=False, quiet=True)
    except Exception:
        _restore_backups(backups)
        raise
    post_ver, _ = check_config_version()
    if post_ver < latest_ver:
        restored = _restore_backups(backups)
        raise RuntimeError(
            f"migration did not advance config version to {latest_ver} "
            f"(still {post_ver}); restored: "
            + (", ".join(str(p) for p in restored) if restored else "none")
        )
    return {"ok": True, "migrated": f"{current_ver}->{latest_ver}"}


# ---------------------------------------------------------------------------
# skills sync (this home only — profiles self-serve on their own boot)
# ---------------------------------------------------------------------------

def step_sync_skills() -> dict:
    """Sync bundled skills into the active home. Content-diffed, respects
    user modifications and deletions; converges on repeat runs."""
    from tools.skills_sync import sync_skills

    result = sync_skills(quiet=True) or {}
    return {
        "ok": True,
        "copied": len(result.get("copied") or []),
        "updated": len(result.get("updated") or []),
    }


# ---------------------------------------------------------------------------
# state.db integrity guard (#68474 — check-only variant)
# ---------------------------------------------------------------------------

def step_state_db_guard() -> dict:
    """Verify the active home's state.db is intact.

    Boot bootstrap has no pre-update snapshot to restore from (that pairing
    lives in ``hermes update``), so this is detection: a corrupt db is
    surfaced loudly in the log instead of the user silently losing session
    search. Read-only, idempotent.
    """
    from hermes_constants import get_hermes_home
    from hermes_cli.backup import verify_sqlite_integrity

    state_path = get_hermes_home() / "state.db"
    if not state_path.exists():
        return {"ok": True, "skipped": "no-state-db"}
    result = verify_sqlite_integrity(state_path, check_header=True, run_pragma=True)
    if result.get("valid"):
        return {"ok": True}
    message = result.get("message", "unknown error")
    logger.error(
        "state.db failed integrity check after a code update: %s — "
        "restore a backup with `hermes backup` tooling or contact support",
        message,
    )
    return {"ok": False, "error": message}


def step_adopt_blessed_checkout() -> dict:
    """One-time adoption of shipped stampless installs (birth certificate).

    Main-era curl|sh / Setup installs created a ``.git`` checkout at a
    blessed managed root but never wrote a stamp — under the stamp-pure
    ladder they would all classify as "somebody's working tree" and
    `hermes update` would refuse them. This step writes the missing fact
    exactly once: blessed root + ``.git`` + no stamp → a minimal stamp
    with ``updateMechanism: self``.

    The blessed-root table lives HERE and only here — it is a one-time
    birth certificate for shipped installs, not a classification rung
    (installation/tree.py never path-matches). Once pre-stamp installs
    are extinct this step and the table can be deleted (TODO.md).

    * ``.git`` anywhere else → never adopted.
    * An existing stamp (any content) → untouched.
    * nix/docker/sealed populations are excluded by construction: their
      update mechanisms replace the tree wholesale with a
      build-time-stamped one, and sealed payloads always ship stamps.
    * Read-only tree (nix-like) → soft skip with a debug log, no crash.
    """
    import json
    import tempfile

    from hermes_constants import get_hermes_home
    from installation.paths import get_install_root

    root = get_install_root()

    # The blessed roots: the canonical locations installers create.
    # (The same table installation/tree.py's ladder used to path-match;
    # it survives only in this adoption step.)
    blessed = (
        get_hermes_home() / "hermes-agent",
        Path("/usr/local/lib/hermes-agent"),
    )

    if not (root / ".git").exists():
        return {"ok": True, "skipped": "not-a-checkout"}
    stamp_path = root / "install-stamp.json"
    if stamp_path.exists():
        return {"ok": True, "skipped": "already-stamped"}

    resolved_root = None
    try:
        resolved_root = root.resolve()
    except OSError:
        return {"ok": True, "skipped": "unresolvable-root"}
    is_blessed = False
    for candidate in blessed:
        try:
            if resolved_root == candidate.resolve():
                is_blessed = True
                break
        except OSError:
            continue
    if not is_blessed:
        return {"ok": True, "skipped": "not-a-blessed-root"}

    stamp = {
        "schemaVersion": 2,
        "updateMechanism": "self",
        "source": "adoption",
        "adoptedAt": datetime.now(timezone.utc).isoformat(),
    }
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(root), prefix=".install-stamp.", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(stamp, indent=2) + "\n")
        os.replace(tmp_name, stamp_path)
    except OSError as exc:
        # A read-only tree (nix-like layouts without their own stamp)
        # must not crash the boot — it just stays unadopted.
        logger.debug("blessed-checkout adoption skipped (unwritable): %s", exc)
        return {"ok": True, "skipped": f"unwritable: {exc}"}
    logger.info("adopted blessed checkout at %s (updateMechanism: self)", root)
    return {"ok": True, "adopted": str(root)}


def step_provision_runtimes() -> dict:
    """Provision managed runtime tools (node, npm, uv, git, gh, ripgrep) into
    the install-scoped runtime dir from runtime-pins.json. THE dep engine
    for updates AND fresh installs (the installers run
    ``python -m installation.provisioner`` directly); see
    installation/provisioner.py."""
    from installation.provisioner import step_provision_runtimes as _run

    return _run()


def step_expose_cli() -> dict:
    """Keep the user-facing ``hermes`` launchers alive across updates.

    The installers write POSIX wrapper scripts into the link dir
    (``~/.local/bin`` and friends) exactly once, at install time — so a
    moved checkout, a recreated venv, or a user's stray ``rm`` leaves
    stale or missing launchers that nothing repairs until a full
    reinstall. This step makes the POST-UPDATE side own the recurring
    maintenance: rewrite the three wrappers (hermes, hermes-agent,
    hermes-acp) whenever their recorded shape drifts from what this
    tree would write today. First-time PATH bootstrapping (shell-rc
    edits, Windows registry) stays installer-side on purpose — a
    boot-time step must not edit rc files on every update.

    Config-gated by ``cli.expose_on_path`` (default true). Windows is a
    no-op for now: venv Scripts are already User-PATH-persisted by the
    installer, and signed-trampoline copying (bundled installs) lands
    with the desktop payload work.
    """
    if sys.platform == "win32":
        return {"ok": True, "skipped": "windows-installer-owned"}

    try:
        from hermes_cli.config import load_config

        cli_cfg = (load_config() or {}).get("cli", {})
        if isinstance(cli_cfg, dict) and not bool(cli_cfg.get("expose_on_path", True)):
            return {"ok": True, "skipped": "config-disabled"}
    except Exception as exc:  # noqa: BLE001 — config trouble must not kill the step
        logger.debug("Could not read cli.expose_on_path: %s", exc)

    from installation.paths import get_install_root
    from installation.tree import is_bundled_payload

    root = get_install_root()

    # Shape FIRST, capability second. The stamp is the shape authority
    # (the same fact the desktop shell's installShape() gates on): a
    # bundled payload never gets installer-written wrappers — its
    # launchers SHIP in agent-payload/bin as prebuilt signed shims.
    # macOS is the one platform where nothing at install time can expose
    # them (a dragged .app runs no installer), so there — and only there
    # — link the user's bin dir at the bundle's own shims. Windows
    # bundled exposure is installer-owned (NSIS PATH / MSIX aliases) and
    # never reaches here (win32 returned above); a Linux AppImage mounts
    # at a transient path a symlink to which would dangle the moment the
    # app exits.
    if is_bundled_payload(root):
        if sys.platform == "darwin":
            return _symlink_sealed_launchers(root.parent / "bin")
        return {"ok": True, "skipped": "bundle-owns-launchers"}

    # Capability probe for the checkout shape: the wrapper bodies bake
    # these two paths into text, so both must exist to have anything to
    # point at. A checkout with a nuked venv lands here — skip; the
    # installer/bootstrap owns venv repair, not this step.
    venv_python = root / "venv" / "bin" / "python"
    entrypoint = root / "hermes"
    if not venv_python.is_file() or not entrypoint.is_file():
        return {"ok": True, "skipped": "no-venv-layout"}

    link_dir = Path.home() / ".local" / "bin"
    wrappers = {
        "hermes": f'exec "{venv_python}" "{entrypoint}" "$@"',
        "hermes-agent": f'exec "{venv_python}" "{root / "run_agent.py"}" "$@"',
        "hermes-acp": f'exec "{venv_python}" "{entrypoint}" acp "$@"',
    }

    written: list[str] = []
    try:
        link_dir.mkdir(parents=True, exist_ok=True)
        for name, exec_line in wrappers.items():
            target = link_dir / name
            body = (
                "#!/usr/bin/env bash\n"
                "unset PYTHONPATH\n"
                "unset PYTHONHOME\n"
                f"{exec_line}\n"
            )
            try:
                existing = target.read_text(encoding="utf-8-sig")
            except (FileNotFoundError, UnicodeDecodeError, OSError):
                existing = None
            if existing == body:
                continue  # current — do not churn mtimes every boot
            # A launcher pointing at ANOTHER install is the user's own
            # arrangement (two checkouts, one link dir) — leave it alone.
            # Ours means: mentions this root in its text, OR is a symlink
            # resolving into this root (the pre-#21454 install shape,
            # where the link dir pointed straight at the venv console
            # script — reading THROUGH it shows no path at all).
            is_symlink_into_root = (
                target.is_symlink()
                and str(target.resolve()).startswith(str(root) + os.sep)
            )
            if (
                existing is not None
                and existing.strip()
                and str(root) not in existing
                and not is_symlink_into_root
            ):
                continue
            # The installers' #21454 lesson: clear first, so writing can
            # never follow an old symlink into the venv and clobber a
            # console script.
            target.unlink(missing_ok=True)
            target.write_text(body, encoding="utf-8")
            target.chmod(0o755)
            written.append(name)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "written": written}


def _symlink_sealed_launchers(payload_bin) -> dict:
    """Link ~/.local/bin/{hermes,hermes-agent,hermes-acp} at a sealed
    bundle's own prebuilt shims (macOS only).

    Symlinks, not copies: the shims are signed as part of the app bundle,
    and a copy would both orphan the signature's context and go stale on
    every app update — a symlink into the .app follows the bundle's
    content wherever Squirrel.Mac swaps it. The shim resolves its own
    REAL path (canonicalized), so the sidecar lookup happens inside the
    bundle, not in ~/.local/bin.

    Ownership guard mirrors step_expose_cli's wrapper logic: an existing
    entry is replaced only when it is ours — a symlink into THIS app
    bundle's payload — or missing/broken. A user's own `hermes` (pipx,
    another checkout's wrapper) is never touched.
    """
    link_dir = Path.home() / ".local" / "bin"
    payload_root = payload_bin.parent
    written: list[str] = []
    try:
        link_dir.mkdir(parents=True, exist_ok=True)
        for name in ("hermes", "hermes-agent", "hermes-acp"):
            source = payload_bin / name
            if not source.is_file():
                continue
            target = link_dir / name
            if target.is_symlink():
                current = os.readlink(target)
                if current == str(source):
                    continue  # already ours and current
                # Ours if it points into this payload (stale app path from
                # a previous version counts — resolve() of a dangling link
                # still yields the old path text) — or dangling entirely.
                points_into_payload = str(Path(current)).startswith(str(payload_root) + os.sep)
                if not points_into_payload and target.exists():
                    continue  # a live foreign link — user's arrangement
            elif target.exists():
                continue  # a real file we did not write — never clobber
            target.unlink(missing_ok=True)
            target.symlink_to(source)
            written.append(name)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "written": written, "mode": "sealed-symlinks"}


# ---------------------------------------------------------------------------
# step registries — boot_bootstrap gates each list with the matching record
# ---------------------------------------------------------------------------

HOME_STEPS: tuple = (
    ("adopt_blessed_checkout", step_adopt_blessed_checkout),
    ("migrate_config", step_migrate_config),
    ("sync_skills", step_sync_skills),
    ("state_db_guard", step_state_db_guard),
    ("expose_cli", step_expose_cli),
)

# Machine steps may be slow (network installers); boot bootstrap runs them
# AFTER writing the machine record, detached from boot readiness.
#
# There is no separate cua-driver refresh step: the driver is a pinned
# managed tool, so provision_runtimes carries a pin bump onto any install
# whose facts already record it — the same way it carries node or ripgrep.
MACHINE_STEPS: tuple = (
    ("provision_runtimes", step_provision_runtimes),
)


def run_steps(steps: Iterable) -> dict:
    """Run steps in order; one failure never stops the rest.

    Returns ``{name: result_dict}``. A raising step records
    ``{"ok": False, "error": str}`` — the caller still writes its record so
    a broken step cannot retrigger the slow path on every boot.
    """
    results: dict = {}
    for name, func in steps:
        try:
            results[name] = func()
        except Exception as exc:
            logger.warning("post-update step %s failed: %s", name, exc)
            results[name] = {"ok": False, "error": str(exc)}
    return results


def resync_and_reexec(args) -> int | None:
    """Phase 1 of the update-phase: own the venv sync, then hand off.

    Runs in whatever interpreter called us — usually the venv python the
    tree swap just invalidated — so it touches as little as possible:
    ``venv_sync`` (stdlib-only by contract) decides from the lockfile
    digest whether the venv is stale, syncs it via managed uv when it
    is, and then this process REPLACES ITSELF with a fresh interpreter
    that has never mapped a pre-sync module.

    Returns None to mean "you are already the fresh process — run phase
    2", or an exit code to propagate.

    The boundary is double-guarded (§B decision):

    * the ``--resumed-after-sync`` argv flag is the loop-proofing — the
      exec'd child must not sync again even if another writer moves the
      stamp between exec and check, because a flag in argv cannot race;
    * the venv_sync stamp is the idempotence — a re-run of the whole
      update sees a fresh stamp and skips the sync entirely.

    POSIX uses ``os.execv``: same pid, so the update-lock marker's owner
    stays literally correct. Windows has no true exec — spawn + wait +
    propagate, and the child passes the lock by process ancestry exactly
    as the ``--update-phase`` spawn already does.
    """
    if args.resumed_after_sync:
        return None

    from hermes_cli import venv_sync

    result = venv_sync.sync()
    state = result.get("state")
    if state == "failed":
        print(f"  ✗ venv sync failed: {result.get('detail')}")
        return 1
    if state in ("sealed", "current"):
        # Nothing changed under us — this interpreter is as good as a
        # fresh one, and an exec would only cost startup time.
        return None

    print("  ✓ venv synced — handing off to a fresh interpreter")
    argv = [sys.executable, "-m", "hermes_cli.post_update", "--update-phase",
            "--resumed-after-sync"]
    if args.gateway_mode:
        argv.append("--gateway-mode")
    if args.assume_yes:
        argv.append("--assume-yes")
    if args.pre_update_snapshot_id:
        argv.extend(["--pre-update-snapshot-id", str(args.pre_update_snapshot_id)])

    if os.name == "posix":
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, argv)
        # unreachable: execv only returns by raising

    completed = subprocess.run(argv)
    return completed.returncode


def main(argv: list | None = None) -> int:
    """``python -m hermes_cli.post_update`` — run in a FRESH interpreter
    so every step imports post-pull code (no reload lists).

    Two modes:

    * default / ``--scope``: the boot-bootstrap step registries.
    * ``--update-phase``: the full ``hermes update`` post-update phase
      (``update_cmd._run_update_phase_inline``) — config prompt/migration,
      skills sync, state.db guard, notices, self-heals, cua refresh, and
      the gateway fleet restart. ``hermes update`` spawns this with
      inherited stdio; the desktop's streamed-update consumer forwards
      our lines unchanged. Phase 1 (``resync_and_reexec``) syncs the venv
      first and re-execs, so phase 2 always runs on the synced world.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="hermes_cli.post_update")
    parser.add_argument("--scope", choices=("home", "machine", "all"), default="all")
    parser.add_argument("--update-phase", action="store_true")
    parser.add_argument("--gateway-mode", action="store_true")
    parser.add_argument("--assume-yes", action="store_true")
    parser.add_argument("--pre-update-snapshot-id", default=None)
    parser.add_argument("--pre-update-version", default=None)
    parser.add_argument(
        "--resumed-after-sync",
        action="store_true",
        help="internal: this process IS the post-sync interpreter; never sync again",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.update_phase:
        handoff = resync_and_reexec(args)
        if handoff is not None:
            return handoff

        # This process was just born, so update_cmd and everything it
        # imports come from the pulled tree. The in-function reload
        # band-aids (_reload_config_modules) turn into no-ops here —
        # the "fresh" modules ARE the loaded modules.
        from hermes_cli.update_cmd import _run_update_phase_inline

        return _run_update_phase_inline(
            gateway_mode=args.gateway_mode,
            assume_yes=args.assume_yes,
            pre_update_snapshot_id=args.pre_update_snapshot_id,
            windows_gateway_resume=None,
            pre_update_version=args.pre_update_version,
        )

    selected: list = []
    if args.scope in ("home", "all"):
        selected.extend(HOME_STEPS)
    if args.scope in ("machine", "all"):
        selected.extend(MACHINE_STEPS)
    results = run_steps(selected)
    failed = [name for name, res in results.items() if not res.get("ok")]
    for name, res in results.items():
        state = "ok" if res.get("ok") else f"FAILED ({res.get('error')})"
        skipped = res.get("skipped")
        print(f"  post-update {name}: {f'skipped ({skipped})' if skipped else state}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
