"""Sync the install's Python environment to its tree — and nothing else.

One of the two separately-invocable, stdlib-only halves of "make this
install runnable" (doc 3 round 3):

* ``installation.provisioner`` — pinned tools, shape-universal.
* THIS module — the venv, whose meaning depends on the install shape:

  - **checkout** (git clone + venv): run ``uv sync`` against the tree's
    ``pyproject.toml``/``uv.lock``. This is the only hash-verified path —
    the lockfile pins a SHA256 for every transitive, so a worm-poisoned
    release that PyPI has not yet quarantined is rejected instead of
    installed.
  - **sealed** (desktop bundle, nix, docker once unified): the base
    interpreter tree is a build artifact — syncing it is not possible
    and not meaningful. Exit 0 with ``{"state": "sealed"}``, cleanly.
    (The lazy-deps overlay half lands with the per-install state folder:
    a bundle swap must re-sync installed features against the new base
    tree. Until ``features.json`` exists there is nothing to read.)

Stdlib-only is a hard contract, enforced by the same AST audit that
covers ``installation``: this runs on freshly-cloned trees where the
venv does not exist yet, and after tree swaps where the venv is not
trustworthy — exactly the moments a third-party import would explode.

Why the sync is not just "run uv every time": ``uv sync`` on an
already-current venv still costs ~1-2s of resolver work, and the boot
path calls this through ``post_update``. The lockfile digest recorded in
``.hermes-runtime/cache/venv-sync.json`` makes currency a file read.

Invocation:

    python -m hermes_cli.venv_sync                # sync if stale
    python -m hermes_cli.venv_sync --check        # report, change nothing
    python -m hermes_cli.venv_sync --json         # machine-readable

Exit 0: current/synced/sealed. Exit 1: a sync was needed and failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# Same extra the installers use: the curated [all], never --all-extras
# (which would drag in [matrix]'s python-olm build and [rl]'s git deps).
SYNC_EXTRA = "all"
STAMP_NAME = "venv-sync.json"
STAMP_SCHEMA = 1


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _runtime_cache(project_root: Path) -> Path:
    # Lazy import: the stdlib-only audit checks what a bare IMPORT of this
    # module loads. installation.paths is itself stdlib-only (same audit),
    # and it owns the runtime-dir layout — including the install stamp's
    # runtimeDir for sealed payloads — so the name never gets restated here.
    from installation.paths import get_runtime_dir

    return get_runtime_dir(project_root) / "cache"


def _stamp_path(project_root: Path) -> Path:
    return _runtime_cache(project_root) / STAMP_NAME


def _lock_digest(project_root: Path) -> str | None:
    """Content hash of what a sync would consume.

    pyproject.toml is part of the key: an extras edit without a lock
    bump must re-sync (same reasoning as ``_npm_manifests_digest`` on
    the node side).
    """
    h = hashlib.sha256()
    found = False
    for name in ("uv.lock", "pyproject.toml"):
        try:
            h.update((project_root / name).read_bytes())
            found = True
        except OSError:
            h.update(b"-")
    return h.hexdigest() if found else None


def _is_sealed(project_root: Path) -> bool:
    """A sealed tree ships its interpreter; only checkouts own a venv.

    The stamp file is the authority (installation.tree reads the same
    file, but importing it here would drag the ``installation`` package
    into this module's audit surface for one JSON read). A tree with
    BOTH a stamp and .git is a dev tree — treat as checkout.

    Mirrors read_build_info()'s hard-fail: a stamp without a valid
    ``updateMechanism`` is a build-lane bug and must not be silently
    read as "not sealed" (that is exactly the misclassification that
    made sealed trees look updatable).
    """
    if (project_root / ".git").exists():
        return False
    try:
        data = json.loads(
            (project_root / "install-stamp.json").read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError):
        return False
    if not (isinstance(data, dict) and bool(data)):
        return False
    if data.get("updateMechanism") not in ("self", "electron-updater", "external"):
        raise RuntimeError(
            f"install-stamp.json at {project_root} is missing a valid "
            "'updateMechanism' (one of self, electron-updater, external). The "
            "build lane that wrote this stamp must pass --update-mechanism to "
            "scripts/write_install_stamp.py."
        )
    return True


def _managed_uv(project_root: Path) -> str | None:
    """The pinned uv, resolved the way the store resolves everything.

    ``installation.uv.uv_path`` IS that resolution — facts from the
    runtime dir (full ladder: HERMES_RUNTIME_DIR, the install stamp's
    runtimeDir, ``<root>/.hermes-runtime``), bytes from the store the
    facts point at. ``installation`` is under the same stdlib-only audit
    as this module, so the import costs nothing.
    """
    from installation.uv import uv_path

    uv = uv_path()
    return str(uv) if uv is not None else None


def read_stamp(project_root: Path) -> dict:
    try:
        data = json.loads(_stamp_path(project_root).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_stamp(project_root: Path, digest: str) -> None:
    path = _stamp_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "schemaVersion": STAMP_SCHEMA,
                "lockDigest": digest,
                "python": sys.version.split()[0],
            }
        ),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def sync(project_root: Path | None = None, *, check: bool = False) -> dict:
    """Bring the venv up to the tree. Returns a state dict, never raises.

    States: ``sealed`` (nothing to sync, by design), ``current`` (stamp
    matches the lockfile digest), ``synced`` (uv ran and the stamp moved),
    ``failed`` (uv ran and did not converge — detail says why),
    ``would-sync`` (check mode found staleness and stopped).
    """
    root = Path(project_root) if project_root else _project_root()

    if _is_sealed(root):
        return {"state": "sealed", "ok": True}

    digest = _lock_digest(root)
    if digest is None:
        return {
            "state": "failed",
            "ok": False,
            "detail": f"no pyproject.toml or uv.lock under {root}",
        }

    if read_stamp(root).get("lockDigest") == digest:
        return {"state": "current", "ok": True}

    if check:
        return {"state": "would-sync", "ok": True}

    uv = _managed_uv(root)
    if uv is None:
        return {
            "state": "failed",
            "ok": False,
            "detail": "managed uv not provisioned (run the provisioner first)",
        }

    env = dict(os.environ)
    env["UV_PROJECT_ENVIRONMENT"] = str(root / "venv")
    # A stale VIRTUAL_ENV from the calling shell would win over the
    # project environment and sync the WRONG venv.
    env.pop("VIRTUAL_ENV", None)

    cmd = [uv, "sync", "--extra", SYNC_EXTRA]
    if (root / "uv.lock").is_file():
        cmd.append("--locked")
    proc = subprocess.run(cmd, cwd=str(root), env=env)
    if proc.returncode != 0:
        # No stamp write: the next run must try again, not skip.
        return {
            "state": "failed",
            "ok": False,
            "detail": f"uv sync exited {proc.returncode}",
        }

    write_stamp(root, digest)
    return {"state": "synced", "ok": True}


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes_cli.venv_sync")
    parser.add_argument("--project-root", default=None)
    parser.add_argument(
        "--check", action="store_true", help="report; change nothing"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = sync(
        Path(args.project_root) if args.project_root else None, check=args.check
    )

    if args.json:
        print(json.dumps(result))
    else:
        detail = f" ({result['detail']})" if result.get("detail") else ""
        print(f"venv sync: {result['state']}{detail}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
