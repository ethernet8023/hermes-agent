"""ensure(): make the installed state match the lockfile for a package,
and hand back its composed environment."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from pm import paths
from pm.lock import Facts, Lockfile
from pm.package import InstallError, Package, Runner, StatePackage, compose_env
from pm.registry import get_package, walk
from pm.store import Store, current_target, merge_tree

# ``progress(stage, done, total, label)`` — stage is "download" | "unpack",
# label is the archive counter ("1/2") when a package has several. Slow
# lines sit in one stage for minutes, so the byte counters are what prove
# liveness to a UI.


def _artifact_progress(progress, index: int, count: int):
    if progress is None:
        return None
    label = f"{index + 1}/{count}" if count > 1 else ""
    return lambda done, total: progress("download", done, total, label)


def _lockfile() -> Lockfile:
    return Lockfile(paths.lockfile_path())


def _facts() -> Facts:
    return Facts(paths.facts_path())


def _store() -> Store:
    return Store(paths.store_root())


def lazy_installs_allowed() -> bool:
    """Policy: may pm install things on demand right now?

    HERMES_DISABLE_LAZY_INSTALLS is an internal bridge var set by the
    official Docker image and the hermetic test harness. The user-facing
    setting is security.allow_lazy_installs in config.yaml; a config
    system that fails to load counts as ALLOWED only when hermes_cli is
    genuinely absent (bootstrap) — config errors fail closed.
    """
    import os

    if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    try:
        from hermes_cli.config import get_config_value
    except ImportError:
        return True
    try:
        return bool(get_config_value("security.allow_lazy_installs", True))
    except Exception:
        return False


def enabled_extras() -> list[str]:
    """The venv extras recorded in the installed state."""
    return list((_facts().get("venv") or {}).get("extras", []))


def is_installed(name: str) -> bool:
    lockfile = _lockfile()
    return _facts().installed(name, lockfile.version(name), _store().root)


def sealed() -> bool:
    """A bundled payload is read-only: its store sits beside the bundle
    manifest. Asking a sealed install for MORE than it shipped is a
    packaging bug, not something a runtime install can fix."""
    return (paths.store_root().parent / "manifest.json").is_file()


def _refuse_lazy(name: str, what: str) -> InstallError:
    if sealed():
        return InstallError(
            name,
            f"this install is sealed and does not ship: {what}",
            "the bundle bakes its entire tree at build time; rebuild the bundle",
        )
    return InstallError(
        name,
        f"not installed and lazy installs are disabled: {what}",
        "enable security.allow_lazy_installs or run `hermes pm install`",
    )


def _install(
    package: Package,
    lockfile: Lockfile,
    facts: Facts,
    store: Store,
    target: str,
    progress=None,
) -> None:
    version = lockfile.version(package.name)
    if version is None:
        raise InstallError(
            package.name, "not in the lockfile", "add it with `hermes pm lock --bump`"
        )

    reason = package.missing_reason(target)
    if reason is not None:
        raise InstallError(package.name, f"unavailable on {target}: {reason}", "none")

    artifacts = lockfile.artifacts(package.name, target)
    entry_name = package.store_entry(version, target)

    with store.install_lock():
        facts.reload()
        if facts.installed(package.name, version, store.root):
            return
        entry = store.entry(entry_name)
        if store.published(entry_name) and package.verify(entry, target):
            shutil.rmtree(entry, ignore_errors=True)
        if not store.published(entry_name):
            if not artifacts:
                raise InstallError(
                    package.name,
                    f"no artifact for {target} in the lockfile",
                    "run `hermes pm lock --bump` for this package",
                )
            with store.scratch() as scratch:
                staged = scratch / "tree"
                try:
                    for index, artifact in enumerate(artifacts):
                        label = f"{index + 1}/{len(artifacts)}" if len(artifacts) > 1 else ""
                        archive = store.fetch(
                            artifact["url"], artifact["sha256"], scratch,
                            progress=_artifact_progress(
                                progress, index, len(artifacts)),
                        )
                        if progress is not None:
                            progress("unpack", 0, 0, label)
                        if index == 0:
                            package.unpack(archive, staged, target)
                            continue
                        # unpack() empties its destination by contract, so
                        # a second archive must be unpacked apart and moved
                        # in — extracting over `staged` would delete the
                        # first archive's files.
                        extra = scratch / f"extra-{index}"
                        package.unpack(archive, extra, target)
                        merge_tree(extra, staged)
                    package.stage(store, staged, version, target)
                    store.publish(staged, entry_name)
                except InstallError:
                    raise
                except Exception as e:
                    raise InstallError(package.name, f"install failed: {e}") from e

        reason = package.verify(entry, target)
        if reason:
            raise InstallError(package.name, f"published entry failed verification: {reason}")

        previous = (facts.get(package.name) or {}).get("version")
        env = package.env(entry, target)
        facts.record(package.name, version, entry_name, env, store.root)
        if previous and previous != version:
            package.migrate(previous, version)


def _fetch_with_retry(store, url: str, sha256: str, scratch, progress=None, attempts: int = 5):
    """store.fetch with a bounded retry: release-asset CDNs (TUR's pool
    302s to GitHub's) throw transient 404/403 windows at their edges --
    observed live, the identical request green minutes later. The digest
    still proves the bytes; a retry cannot smuggle anything past the pin.
    """
    import time

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return store.fetch(url, sha256, scratch, progress=progress)
        except Exception as exc:  # noqa: BLE001 -- retry any fetch failure
            last = exc
            if attempt + 1 < attempts:
                wait = 30 * (attempt + 1)
                logging.getLogger(__name__).warning(
                    "fetch failed (attempt %d/%d) for %s: %s; retrying in %ds",
                    attempt + 1, attempts, url, exc, wait,
                )
                time.sleep(wait)
    raise last


def stage_only(name: str, target: str, progress=None) -> "Path":
    """Cross-target staging: publish the pinned (package, version, target)
    entry into the store and return its path. No facts are written and no
    Runner is composed -- the staged binaries belong to ANOTHER machine
    (e.g. linux-arm64-bionic .debs staged on a glibc CI host); this host's
    installed-state must not learn about them. Idempotent: an already
    published + verifying entry is returned as-is.
    """
    lockfile = _lockfile()
    facts = _facts()
    store = _store()
    package = get_package(name)
    version = lockfile.version(package.name)
    if version is None:
        raise InstallError(package.name, "not in the lockfile")
    reason = package.missing_reason(target)
    if reason is not None:
        raise InstallError(package.name, f"unavailable on {target}: {reason}")
    if getattr(package, "pin_only", False):
        # A pure pin (e.g. the termux-docker digest): no bytes, no store
        # entry, nothing to verify locally -- the pin IS the artifact.
        return store.root / package.store_entry(version, target)
    artifacts = lockfile.artifacts(package.name, target)
    entry_name = package.store_entry(version, target)
    with store.install_lock():
        entry = store.entry(entry_name)
        if store.published(entry_name) and not package.verify(entry, target):
            shutil.rmtree(entry, ignore_errors=True)
        if not store.published(entry_name):
            if not artifacts:
                raise InstallError(
                    package.name,
                    f"no artifact for {target} in the lockfile",
                    "run `hermes pm lock --bump` for this package",
                )
            with store.scratch() as scratch:
                staged = scratch / "tree"
                for index, artifact in enumerate(artifacts):
                    archive = _fetch_with_retry(
                        store, artifact["url"], artifact["sha256"], scratch,
                        progress=_artifact_progress(progress, index, len(artifacts)),
                    )
                    if index == 0:
                        package.unpack(archive, staged, target)
                    else:
                        extra = scratch / f"extra-{index}"
                        package.unpack(archive, extra, target)
                        merge_tree(extra, staged)
                package.stage(store, staged, version, target)
                store.publish(staged, entry_name)
        reason = package.verify(store.entry(entry_name), target)
        if reason:
            raise InstallError(package.name, f"published entry failed verification: {reason}")
    return store.entry(entry_name)


def ensure(
    name: str,
    *,
    base_env: Optional[dict] = None,
    explicit: bool = False,
    progress=None,
    target: Optional[str] = None,
) -> Runner:
    """``explicit`` marks a deliberate install command (`hermes pm
    install`, `hermes pm bundle`) — those ARE the remedy the lazy-install
    policy names, so the policy does not apply to them.

    ``progress(stage, done, total, label)`` reports the slow parts of an
    install to a UI; see _artifact_progress.
    """
    if isinstance(get_package(name), StatePackage):
        sync_venv(explicit=explicit)
        return Runner(name, compose_env([], base=base_env))

    lockfile = _lockfile()
    facts = _facts()
    store = _store()
    target = target or current_target()

    chain = walk([name])
    missing = [
        p for p in chain if not facts.installed(p.name, lockfile.version(p.name), store.root)
    ]

    if missing and (sealed() or (not explicit and not lazy_installs_allowed())):
        raise _refuse_lazy(name, ", ".join(p.name for p in missing))

    for package in missing:
        _install(package, lockfile, facts, store, target, progress=progress)
    if missing:
        facts.reload()

    diffs = [facts.env_for(p.name, store.root) for p in chain]
    return Runner(name, compose_env(diffs, base=base_env))


def env_for(*names: str, base_env: Optional[dict] = None) -> dict[str, str]:
    """Composed env of already-installed packages only. Never installs,
    never raises on missing packages — they contribute nothing."""
    facts = _facts()
    store = _store()
    diffs: list[dict] = []
    for name in names:
        try:
            chain = walk([name])
        except KeyError:
            continue
        for package in chain:
            if facts.installed(package.name, None, store.root):
                diffs.append(facts.env_for(package.name, store.root))
    return compose_env(diffs, base=base_env)


def sync_venv(extras: Optional[list[str]] = None, *, explicit: bool = False) -> None:
    """Make the venv match uv.lock + the enabled extras. Extras union into
    the installed state (one ledger); no-op when the stamp already matches.
    ``explicit`` marks a deliberate install command (`hermes pm install`,
    `hermes update`) — those are the remedy the lazy-install policy points
    at, so the policy does not apply to them."""
    package = get_package("venv")
    facts = _facts()
    fact = facts.get("venv") or {}
    enabled = sorted(set(fact.get("extras", [])) | set(extras or []))
    stamp = package.expected_stamp(enabled)
    if fact.get("stamp") == stamp:
        return
    if sealed() or (not explicit and not lazy_installs_allowed()):
        raise _refuse_lazy("venv", str(extras) if extras else "venv out of sync")
    with _store().install_lock():
        facts.reload()
        fact = facts.get("venv") or {}
        enabled = sorted(set(fact.get("extras", [])) | set(extras or []))
        stamp = package.expected_stamp(enabled)
        if fact.get("stamp") == stamp:
            return
        package.apply(enabled)
        facts.record_state("venv", stamp, enabled)


def adopt() -> bool:
    """First boot of a bundled install: make the shipped payload THIS
    machine's installed state. The payload carries store entries and a
    facts.json whose env values hold `{{store}}` templates — resolution
    happens at read time (env_for), so adoption only needs to (a) verify
    the shipped entries exist and (b) point the relocatable venv's
    pyvenv.cfg at the shipped interpreter. No network, no pip.

    Idempotent and cheap: returns False when there is nothing to adopt
    (not a bundled install, or already adopted)."""
    store = _store()
    facts = _facts()
    if not paths.facts_path().is_file():
        return False

    marker = store.root.parent / ".adopted"
    if marker.is_file():
        return False

    # "python" becomes a pm package when the payload ships its own
    # interpreter; a bundle without one has nothing to re-point.
    python_fact = facts.get("python")
    venv_dir = store.root.parent / "venv"
    cfg = venv_dir / "pyvenv.cfg"
    if python_fact and cfg.is_file():
        try:
            entry = store.root / python_fact["entry"]
            home = entry if not (entry / "bin").is_dir() else entry / "bin"
            text = cfg.read_text(encoding="utf-8")
            fixed = [
                f"home = {home}" if line.lower().startswith("home =") else line
                for line in text.splitlines()
            ]
            new_text = "\n".join(fixed) + "\n"
            if new_text != text:
                cfg.write_text(new_text, encoding="utf-8")
        except OSError:
            # Best-effort: on a read-only payload (sealed MSIX) the write is
            # impossible, and bundled boots run the store python through the
            # CLI shim anyway (no pyvenv.cfg needed). Adoption must not fail
            # the whole boot over it — the marker below still records that
            # the shipped entries were checked.
            import logging

            logging.getLogger(__name__).debug(
                "pm adopt: could not re-point pyvenv.cfg", exc_info=True
            )

    try:
        marker.write_text("", encoding="utf-8")
    except OSError:
        pass
    return True


def check() -> list[str]:
    """The startup check: cheap stamp comparisons of the installed state
    against the lockfile. Returns problems; empty means healthy. Never
    installs, never touches the network. An install pm has never touched
    (no installed-state file) reports nothing — pm only vouches for what
    it installed. Lockfile packages this build doesn't know (version skew
    during a partial update) are skipped, not fatal."""
    if not paths.facts_path().is_file():
        return []

    problems: list[str] = []
    lockfile = _lockfile()
    facts = _facts()
    store = _store()
    target = current_target()
    for name in lockfile.names():
        try:
            package = get_package(name)
        except KeyError:
            continue
        if package.optional or package.internal:
            continue
        if package.missing_reason(target) is not None:
            continue
        if not facts.installed(name, lockfile.version(name), store.root):
            problems.append(f"{name}: not installed or outdated")
    venv = get_package("venv")
    fact = facts.get("venv")
    if fact is not None:
        expected = venv.expected_stamp(fact.get("extras", []))
        if fact.get("stamp") != expected:
            problems.append("venv: out of sync with uv.lock")
    return problems


def _store_path_dirs() -> list[str]:
    """Composed PATH dirs of all installed (non-internal, on_path) store
    packages, deps-first, deduped. Includes optional packages that are
    *installed* (facts say so) — an installed git/gh must be on PATH even
    though it's not in the root closure. Never installs."""
    import os

    if not paths.facts_path().is_file():
        return []
    facts = _facts()
    store = _store()
    lockfile = _lockfile()
    target = current_target()
    dirs: list[str] = []
    for name in lockfile.names():
        try:
            package = get_package(name)
        except KeyError:
            continue
        if package.internal:
            continue
        if not getattr(package, "on_path", True):
            continue
        if package.missing_reason(target) is not None:
            continue
        if not facts.installed(name, lockfile.version(name), store.root):
            continue
        env = facts.env_for(name, store.root)
        path_dirs = env.get("PATH") or []
        if isinstance(path_dirs, str):
            path_dirs = [path_dirs]
        for directory in path_dirs:
            if directory and directory not in dirs:
                dirs.append(str(directory))
    return dirs


def activate() -> None:
    """Make the installed store usable: prepend its tool dirs to
    os.environ['PATH'] so reactive `shutil.which('git'|'bash'|'ffmpeg'|...)`
    resolves the bundled binaries. The gate is `check()` — if the store is
    broken, refuse to inject (fail fast rather than serving a partial PATH).

    This is the ONE sanctioned global PATH write: PATH is the discovery
    contract every `which` reads, not a tool-specific env leak. Store-first
    unconditionally — pinned bundled versions win on dev machines too.
    """
    import os

    if check():
        return  # broken store → do not provision; callers surface `hermes pm install`
    dirs = _store_path_dirs()
    if not dirs:
        return
    existing = os.environ.get("PATH", "")
    prefix = os.pathsep.join(dirs)
    existing_lower = {p.lower() for p in existing.split(os.pathsep) if p}
    missing = [d for d in dirs if d.lower() not in existing_lower]
    if missing:
        os.environ["PATH"] = os.pathsep.join([*missing, existing]) if existing else os.pathsep.join(missing)



def uv(*, venv=None, realize: bool = True):
    """TRANSITIONAL: (uv path, sanitized env) for call sites that still
    drive uv themselves. Two classes remain: update/repair sites (die with
    the update collapse, plan step 4) and side-venv installs — browser-use
    tool venvs (tools_config, browser_use_cli) and hindsight's
    local_embedded daemon — which survive until pm grows the side-venv
    package kind (plan step 5's remaining half). Must not spread."""
    from pm.packages import uv_env

    env = uv_env()
    if venv is not None:
        env["VIRTUAL_ENV"] = str(venv)
        env.pop("UV_NO_CONFIG", None)

    lockfile = _lockfile()
    facts = _facts()
    store = _store()
    package = get_package("uv")
    if not facts.installed("uv", lockfile.version("uv"), store.root):
        if not realize or not lazy_installs_allowed():
            return None, env
        try:
            _install(package, lockfile, facts, store, current_target())
            facts.reload()
        except Exception:
            import logging

            logging.getLogger(__name__).debug("pm.uv: install failed", exc_info=True)
            return None, env
    fact = facts.get("uv")
    if fact is None:
        return None, env
    binary = package.binary(store.entry(fact["entry"]), current_target())
    if binary is None or not binary.is_file():
        return None, env
    return str(binary), env
