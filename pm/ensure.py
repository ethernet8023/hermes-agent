"""ensure(): make the installed state match the lockfile for a package,
and hand back its composed environment."""

from __future__ import annotations

import shutil
from typing import Optional

from pm import paths
from pm.lock import Facts, Lockfile
from pm.package import InstallError, Package, Runner, StatePackage, compose_env
from pm.registry import get_package, walk
from pm.store import Store, current_target


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


def _install(package: Package, lockfile: Lockfile, facts: Facts, store: Store, target: str) -> None:
    version = lockfile.version(package.name)
    if version is None:
        raise InstallError(
            package.name, "not in the lockfile", "add it with `hermes pm lock --bump`"
        )

    reason = package.missing_reason(target)
    if reason is not None:
        raise InstallError(package.name, f"unavailable on {target}: {reason}", "none")

    sha256 = lockfile.sha256(package.name, target)
    url = lockfile.url(package.name, target)
    entry_name = package.store_entry(version, target)

    with store.install_lock():
        facts.reload()
        if facts.installed(package.name, version, store.root):
            return
        entry = store.entry(entry_name)
        if store.published(entry_name) and not package.verify(entry, target):
            shutil.rmtree(entry, ignore_errors=True)
        if not store.published(entry_name):
            if sha256 is None or url is None:
                raise InstallError(
                    package.name,
                    f"no artifact for {target} in the lockfile",
                    "run `hermes pm lock --bump` for this package",
                )
            with store.scratch() as scratch:
                staged = scratch / "tree"
                try:
                    archive = store.fetch(url, sha256, scratch)
                    package.unpack(archive, staged, target)
                    package.stage(store, staged, version, target)
                    store.publish(staged, entry_name)
                except InstallError:
                    raise
                except Exception as e:
                    raise InstallError(package.name, f"install failed: {e}") from e

        if not package.verify(entry, target):
            raise InstallError(package.name, "published entry failed verification")

        previous = (facts.get(package.name) or {}).get("version")
        env = package.env(entry, target)
        facts.record(package.name, version, entry_name, env, store.root)
        if previous and previous != version:
            package.migrate(previous, version)


def ensure(name: str, *, base_env: Optional[dict] = None, explicit: bool = False) -> Runner:
    """``explicit`` marks a deliberate install command (`hermes pm
    install`, `hermes pm bundle`) — those ARE the remedy the lazy-install
    policy names, so the policy does not apply to them."""
    if isinstance(get_package(name), StatePackage):
        sync_venv(explicit=explicit)
        return Runner(name, compose_env([], base=base_env))

    lockfile = _lockfile()
    facts = _facts()
    store = _store()
    target = current_target()

    chain = walk([name])
    missing = [
        p for p in chain if not facts.installed(p.name, lockfile.version(p.name), store.root)
    ]

    if missing and (sealed() or (not explicit and not lazy_installs_allowed())):
        raise _refuse_lazy(name, ", ".join(p.name for p in missing))

    for package in missing:
        _install(package, lockfile, facts, store, target)
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


def run(name: str, cmd: list, **kwargs):
    """The one way hermes code runs a managed tool. Installs it if missing
    (lazy packages), composes its env, runs. Internal packages (uv, npm)
    are pm implementation details and refused here."""
    package = get_package(name)
    if package.internal:
        raise InstallError(
            name, "internal package", "pm uses this inside install steps only"
        )
    runner = ensure(name)
    binary = None
    fact = _facts().get(name)
    if fact is not None:
        binary = package.binary(_store().entry(fact["entry"]), current_target())
    if binary is None or not binary.is_file():
        raise InstallError(name, "installed but its binary is missing")
    return runner.run([str(binary), *cmd], **kwargs)


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
