"""Boot-time post-update bootstrap.

Every install kind (git checkout, desktop bundled payload, docker, nix)
compares two per-install facts at boot:

* current identity — the commit this install IS: ``install-stamp.json``
  for sealed trees, ``.git/HEAD`` for checkouts. Reading it is a couple of
  file reads, no subprocess.
* last-known identity — the commit this install last bootstrapped, recorded
  under ``install-bootstrap/`` keyed by the canonical install root.

Equal → nothing happens (the fast path, ~2 ms). Different → run the
idempotent post-update steps from ``hermes_cli.post_update`` under a
single-flight lock, then record the new identity.

Two records, one per step scope:

* home record — ``get_hermes_home()/install-bootstrap/<key>.json``.
  Gates home-scoped steps. HERMES_HOME moves per profile, so each profile
  bootstraps its own state once per code change.
* machine record — ``<base home>/install-bootstrap/<key>.machine.json``,
  anchored to the DEFAULT home (HOME-anchored, not HERMES_HOME-anchored —
  the ``_get_profiles_root()`` convention). Every profile resolves the same
  file, so machine-global steps run once per machine per code change and
  the record's lock serializes concurrent profile boots.

The records are an optimization, never the correctness layer: every step is
idempotent and self-gating, so a deleted record costs one redundant slow
path, nothing more.

Design: .hermes/plans/2026-08-10_163500-boot-time-post-update-bootstrap.md.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

RECORD_SCHEMA_VERSION = 1
RECORD_DIR_NAME = "install-bootstrap"
LOCK_STALE_SECONDS = 600


# ---------------------------------------------------------------------------
# current identity
# ---------------------------------------------------------------------------

def read_git_head(root: Path) -> str | None:
    """The commit SHA of the checkout at ``root``.

    Asks git, rather than reimplementing it. The managed git comes first
    (every install kind provisions one) and a PATH git second; with
    neither, the answer is None and boot carries on — the same fail-open
    the file parser had.

    This used to parse ``.git`` by hand: the worktree gitfile, symbolic
    HEAD, loose refs, ``commondir`` delegation and ``packed-refs``. That
    was a reimplementation of ``git rev-parse HEAD`` maintained against
    git's on-disk formats, and reftable (which stores refs in neither
    loose files nor packed-refs) would have broken every line of it.

    Cost: one ~10ms subprocess where the parse was ~2ms of file reads.
    Only checkouts pay it — a sealed tree reads its stamp and never gets
    here — and it happens once per boot.
    """
    git = _git_binary()
    if git is None:
        return None
    try:
        out = subprocess.run(
            [git, "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    sha = out.stdout.strip()
    return sha if len(sha) >= 7 else None


def _git_binary() -> str | None:
    """The git to run: managed first, PATH second, None when neither.

    Managed-first because it is the one we pinned and verified. The
    import is local: ``installation`` reaches the registry facts, and
    boot_bootstrap is imported early enough that a module-level import
    would widen the boot import graph for a function most boots skip.

    The macOS xcode-select shim is not git — it is a stub whose only
    behaviour is to pop an "install developer tools?" dialog, which a
    boot-time path must never trigger.
    """
    try:
        from installation.env import is_macos_xcode_shim, managed_tool_binary

        managed = managed_tool_binary("git")
        if managed is not None:
            return str(managed)
    except Exception as exc:  # noqa: BLE001 — boot must not die on a lookup
        logger.debug("managed git lookup failed: %s", exc)
        is_macos_xcode_shim = None  # type: ignore[assignment]

    found = shutil.which("git")
    if found is None:
        return None
    if is_macos_xcode_shim is not None and is_macos_xcode_shim(found):
        return None
    return found


def current_install_identity(project_root: Path) -> str | None:
    """What code this install is: stamp commit for sealed trees, git HEAD
    for checkouts, None for broken trees (never bootstrap, never write)."""
    from installation.tree import read_build_info

    root = Path(project_root)
    if (root / ".git").exists():
        return read_git_head(root)
    commit = read_build_info(root).get("commit")
    if isinstance(commit, str) and len(commit) >= 7:
        return commit
    # A tagless/commitless stamp is a broken artifact; the tag alone is
    # accepted as a weaker identity (bundled artifacts always carry one).
    tag = read_build_info(root).get("tag")
    return tag if isinstance(tag, str) and tag else None


# ---------------------------------------------------------------------------
# last-known records
# ---------------------------------------------------------------------------

def _install_key(project_root: Path) -> str:
    try:
        canonical = str(Path(project_root).resolve())
    except OSError:
        canonical = str(project_root)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# the per-install state folder: installs/<SHA16>/ under the DEFAULT home
# ---------------------------------------------------------------------------
#
# Install state used to live in five anchors: install-bootstrap records in
# two homes, .hermes-runtime beside the code, a docker env variable, and
# the Electron userData dir. One folder per install key replaces the
# key-suffixed-file convention; the sha16 already existed
# (_install_key), only the bootstrap records used it.

INSTALLS_DIR_NAME = "installs"


def installs_root() -> Path:
    """The parent of every per-install state folder.

    Anchored to the DEFAULT home, not the active profile home: profiles
    share one folder per install, with per-profile bootstrap records as
    files INSIDE it (bootstrap/<profile>.json) rather than per-profile
    folders. That keeps profile semantics while collapsing the anchor
    count to one.
    """
    from hermes_cli.profiles import _get_default_hermes_home

    return _get_default_hermes_home() / INSTALLS_DIR_NAME


def install_state_dir(project_root: Path) -> Path:
    """``installs/<SHA16>/`` for this install. Derivation only — no I/O."""
    return installs_root() / _install_key(project_root)


def ensure_install_dir(project_root: Path) -> Path:
    """The state folder, created with its identity record on first touch.

    install.json is the REVERSE map (sha16 → canonical root) that makes
    orphan GC possible: `hermes doctor` enumerates installs/*/install.json
    and flags entries whose recorded root no longer exists. Written once,
    under the same single-flight lock the records use; steward comes from
    runtime_tree so the record says who owns the tree, not who touched it
    first.
    """
    state = install_state_dir(project_root)
    marker = state / "install.json"
    if marker.is_file():
        return state
    state.mkdir(parents=True, exist_ok=True)
    lock = _RecordLock(state / ".install-json.lock")
    if not lock.acquire():
        return state  # someone else is writing it right now — theirs wins
    try:
        if not marker.is_file():
            from datetime import datetime, timezone

            from installation.tree import Sealed, runtime_tree

            tree = runtime_tree(project_root)
            payload = {
                "root": str(Path(project_root).resolve()),
                "steward": tree.steward if isinstance(tree, Sealed) else "checkout",
                "firstSeen": datetime.now(timezone.utc).isoformat(),
            }
            tmp = marker.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, marker)
    finally:
        lock.release()
    return state


def orphaned_installs() -> list[tuple[Path, str]]:
    """State folders whose recorded root no longer exists.

    ``(folder, recorded_root)`` pairs for `hermes doctor`'s sweep. A
    folder without a readable install.json is orphaned by definition —
    nothing can ever claim it again, because claiming goes through
    ensure_install_dir which writes the record first.
    """
    root = installs_root()
    if not root.is_dir():
        return []
    orphans: list[tuple[Path, str]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            recorded = json.loads(
                (entry / "install.json").read_text(encoding="utf-8")
            ).get("root", "")
        except (OSError, ValueError):
            orphans.append((entry, "<unreadable install.json>"))
            continue
        if not recorded or not Path(recorded).exists():
            orphans.append((entry, recorded or "<empty>"))
    return orphans


def record_path(project_root: Path, scope: str) -> Path:
    """Where the last-known record for ``project_root`` lives.

    Both scopes live INSIDE the per-install state folder now:
    ``bootstrap/machine.json`` for machine scope, ``bootstrap/<profile>.json``
    for home scope — the per-profile semantics ride the FILENAME, not a
    per-profile anchor directory. New-location-only: the old
    ``install-bootstrap/<key>[.machine].json`` spellings are dead markers
    (never read), and an absent record means one redundant slow path,
    which is the designed cost of the no-compat-ladder convention.
    """
    if scope == "home":
        from hermes_cli.profiles import get_active_profile_name

        name = get_active_profile_name() or "default"
        filename = f"{name}.json"
    elif scope == "machine":
        filename = "machine.json"
    else:
        raise ValueError(f"unknown record scope: {scope!r}")
    return install_state_dir(project_root) / "bootstrap" / filename


def read_last_known(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_record(path: Path, identity: str, results: dict) -> None:
    payload = {
        "schemaVersion": RECORD_SCHEMA_VERSION,
        "identity": identity,
        "bootstrappedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "results": results,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_record(project_root: Path, scope: str, identity: str, results: dict | None = None) -> None:
    """Record ``identity`` as bootstrapped. Also used by ``hermes update``
    after it runs the steps itself, so the next boot skips."""
    _write_record(record_path(project_root, scope), identity, results or {})


def needs_bootstrap(project_root: Path, scope: str) -> str | None:
    """The new identity when this install changed since its last bootstrap,
    else None. None identity (broken tree) never bootstraps."""
    identity = current_install_identity(project_root)
    if not identity:
        return None
    known = read_last_known(record_path(project_root, scope))
    if known.get("identity") == identity:
        return None
    return identity


# ---------------------------------------------------------------------------
# single-flight lock
# ---------------------------------------------------------------------------

class _RecordLock:
    """O_CREAT|O_EXCL existence-as-mutex next to a record file.

    Losers skip (boot never waits on another process's bootstrap; the steps
    are idempotent, so a botched winner only costs redundant work later).
    A stale lock — older than LOCK_STALE_SECONDS — is broken and re-tried
    once: a crashed winner died before its record write, so re-running is
    correct.
    """

    def __init__(self, record: Path):
        self.path = record.with_name(record.name + ".lock")
        self.acquired = False

    def _try_create(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            os.write(fd, json.dumps({"pid": os.getpid(), "startedAt": time.time()}).encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def _is_stale(self) -> bool:
        try:
            body = json.loads(self.path.read_text(encoding="utf-8"))
            started = float(body.get("startedAt", 0))
        except (OSError, ValueError):
            # Unreadable lock: age it by mtime instead.
            try:
                started = self.path.stat().st_mtime
            except OSError:
                return False
        return (time.time() - started) > LOCK_STALE_SECONDS

    def acquire(self) -> bool:
        if self._try_create():
            self.acquired = True
            return True
        if self._is_stale():
            try:
                self.path.unlink()
            except OSError:
                return False
            if self._try_create():
                self.acquired = True
                return True
        return False

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self.acquired = False


# ---------------------------------------------------------------------------
# the boot entry point
# ---------------------------------------------------------------------------

def _report_sealed_runtime_drift(project_root: Path) -> str | None:
    """Check a SEALED tree's managed tools against its pin table, loudly.

    ``require_current_runtimes`` is the artifact-time gate (docker build,
    nix check, desktop payload staging). This is the boot-time backstop
    for artifacts assembled around those gates: every boot of a drifted
    sealed tree prints the steward message to stderr, so the drift is
    impossible to not-know about.

    Report, not refusal: this runs inside the never-raises boot path, and
    a sealed gateway that boots on stale tools is degraded — but a
    gateway that refuses to boot over a tool version is DOWN, remotely,
    with the fix (rebuild the artifact) out of the machine's own reach.
    The message names the steward; the steward's own gate is the wall.

    Returns the message when drift was found (for the boot summary), None
    otherwise. Checkouts return None without reading anything — they
    provision on demand and drift is their normal, self-healing state.
    """
    try:
        from installation.provisioner import (
            StaleManagedRuntimes,
            require_current_runtimes,
        )
    except Exception as exc:  # noqa: BLE001 — a backstop must not become a gate
        logger.debug("sealed runtime drift check unavailable: %s", exc)
        return None
    try:
        require_current_runtimes(project_root=project_root)
    except StaleManagedRuntimes as exc:
        print(f"\n✗ {exc}\n", file=sys.stderr)
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("sealed runtime drift check failed: %s", exc)
    return None


def run_boot_bootstrap(project_root: Path) -> dict:
    """Run due home- and machine-scoped steps for this install. Returns a
    summary dict (for tests/logs); use maybe_run_boot_bootstrap at call
    sites."""
    from hermes_cli import post_update

    summary: dict = {"home": "skipped", "machine": "skipped"}

    drift_message = _report_sealed_runtime_drift(Path(project_root))
    if drift_message:
        summary["sealed_runtime_drift"] = drift_message

    for scope, steps, deferred in (
        ("home", post_update.HOME_STEPS, False),
        ("machine", post_update.MACHINE_STEPS, True),
    ):
        identity = needs_bootstrap(project_root, scope)
        if not identity:
            continue
        record = record_path(project_root, scope)
        lock = _RecordLock(record)
        if not lock.acquire():
            summary[scope] = "lost-race"
            continue
        try:
            # Double-check under the lock: the previous holder may have
            # finished between our read and our acquire.
            if read_last_known(record).get("identity") == identity:
                summary[scope] = "done-by-other"
                continue
            logger.info(
                "post-update bootstrap (%s scope): code changed to %s, running steps",
                scope, identity[:12],
            )
            if deferred:
                # Slow machine steps (network installers) must not block
                # boot readiness: record first, then run detached. A crash
                # mid-step leaves the record written — intended: the record
                # gates "did we trigger for this identity", and the steps
                # re-gate themselves (confirmed-update checks) next change.
                _write_record(record, identity, {"deferred": True})
                import threading

                threading.Thread(
                    target=post_update.run_steps,
                    args=(steps,),
                    name=f"hermes-bootstrap-{scope}",
                    daemon=True,
                ).start()
                summary[scope] = "deferred"
            else:
                results = post_update.run_steps(steps)
                _write_record(record, identity, results)
                summary[scope] = results
        finally:
            lock.release()
    return summary


def maybe_run_boot_bootstrap(project_root: Path) -> None:
    """The one call boot paths use. Never raises: a bootstrap problem must
    not stop the gateway/serve/CLI from starting."""
    try:
        run_boot_bootstrap(Path(project_root))
    except Exception as exc:
        logger.warning("boot bootstrap failed (continuing boot): %s", exc)
