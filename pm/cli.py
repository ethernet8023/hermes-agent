"""hermes pm: lock / install / env / doctor / gc."""

from __future__ import annotations

import argparse
import json
import shutil
import sys

from pm.ensure import _facts, _lockfile, _store, ensure
from pm.package import InstallError
from pm.registry import get_package
from pm.store import ALL_TARGETS, current_target, hash_url


def cmd_lock(args) -> int:
    """--bump <name> <version>: fetch every target's artifact, hash, write."""
    lockfile = _lockfile()
    package = get_package(args.name)
    hashes: dict[str, str] = {}
    targets = list(package.targets) if package.targets is not None else list(ALL_TARGETS)

    for target in targets:
        if package.missing_reason(target) is not None:
            continue
        url = package.fetch_url(args.version, target)
        print(f"  {target}: {url}")
        hashes[target] = hash_url(url)
        print(f"    sha256 {hashes[target]}")
    lockfile.set_pin(args.name, args.version, hashes)
    lockfile.save()
    print(f"pinned {args.name} {args.version} ({len(hashes)} targets)")
    return 0


def cmd_install(args) -> int:
    names = args.names or [
        n for n in _lockfile().names() if not get_package(n).optional
    ]
    failed = 0
    for name in names:
        try:
            ensure(name)
            print(f"✓ {name}")
        except InstallError as e:
            print(f"✗ {e}")
            failed += 1
    if not args.names:
        from pm.ensure import sync_venv

        try:
            sync_venv(explicit=True)
            print("✓ venv")
        except InstallError as e:
            print(f"✗ {e}")
            failed += 1
    return 1 if failed else 0


def cmd_env(args) -> int:
    from pm.ensure import env_for

    names = args.names or _lockfile().names()
    print(json.dumps(env_for(*names), indent=2, sort_keys=True))
    return 0


def cmd_doctor(args) -> int:
    lockfile = _lockfile()
    facts = _facts()
    store = _store()
    target = current_target()
    bad = 0
    for name in lockfile.names():
        package = get_package(name)
        reason = package.missing_reason(target)
        if reason is not None:
            print(f"- {name}: n/a on {target} ({reason})")
            continue
        if not facts.installed(name, lockfile.version(name), store.root):
            state = "not installed" if facts.get(name) is None else "outdated"
            print(f"{'?' if package.optional else '✗'} {name}: {state}")
            bad += 0 if package.optional else 1
            continue
        entry = store.entry(facts.get(name)["entry"])
        if not package.verify(entry, target):
            print(f"✗ {name}: installed but failed verification")
            bad += 1
            continue
        print(f"✓ {name} {facts.get(name)['version']}")
    return 1 if bad else 0


def cmd_gc(args) -> int:
    facts = _facts()
    store = _store()
    if not store.root.is_dir():
        return 0
    keep = facts.entries_in_use()
    removed = 0
    for item in sorted(store.root.iterdir()):
        if not item.is_dir() or item.name.startswith("."):
            continue
        if item.name in keep:
            continue
        print(f"removing {item.name}")
        shutil.rmtree(item, ignore_errors=True)
        removed += 1
    print(f"gc: removed {removed}, kept {len(keep)}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hermes pm")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lock", help="write versions+hashes into pm/lock.json")
    p.add_argument("--bump", dest="name", required=True)
    p.add_argument("version")
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("install", help="install packages (default: all required)")
    p.add_argument("names", nargs="*")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("env", help="print composed env of installed packages")
    p.add_argument("names", nargs="*")
    p.set_defaults(func=cmd_env)

    p = sub.add_parser("doctor", help="check installed state against the lockfile")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("gc", help="remove store entries nothing references")
    p.set_defaults(func=cmd_gc)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
