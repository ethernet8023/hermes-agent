"""hermes pm: lock / install / env / doctor / gc / bundle."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from pm.ensure import _facts, _lockfile, _store, ensure
from pm.ensure import uv as pm_uv
from pm.package import InstallError
from pm.registry import get_package
from pm.store import ALL_TARGETS, current_target, hash_url


def cmd_lock(args) -> int:
    """--bump <name> <version>: resolve every target's archives, hash them,
    write. A target with one archive pins the object; several pin a list.
    Target-independent urls collapse to one "any" artifact."""
    lockfile = _lockfile()
    package = get_package(args.name)
    artifacts: dict[str, object] = {}

    def pin(url: str) -> dict:
        print(f"    {url}")
        digest = package.known_sha256(args.version, url) or hash_url(url)
        print(f"      sha256 {digest}")
        return {"url": url, "sha256": digest}

    urls = {
        target: package.fetch_urls(args.version, target)
        for target in ALL_TARGETS
        if package.missing_reason(target) is None
    }
    distinct = {tuple(u) for u in urls.values()}
    if len(distinct) == 1:
        print("  any:")
        pinned = [pin(url) for url in next(iter(urls.values()))]
        artifacts["any"] = pinned[0] if len(pinned) == 1 else pinned
    else:
        for target, target_urls in urls.items():
            print(f"  {target}:")
            pinned = [pin(url) for url in target_urls]
            artifacts[target] = pinned[0] if len(pinned) == 1 else pinned
    lockfile.set_pin(args.name, args.version, artifacts)
    lockfile.save()
    print(f"pinned {args.name} {args.version} ({len(artifacts)} targets)")
    return 0


def _fmt_bytes(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MiB"


def _live_progress(name: str):
    """Per-package progress for ensure(): download as % + MiB, unpack as a
    phase line. Throttled to ~4 MiB steps — a slow line proves it's moving
    in a piped (CI) log without flooding it (a 1 MiB tick on a 1.5 GiB
    model would be ~1,500 lines)."""
    last = 0

    def report(stage: str, done: int, total: int, label: str) -> None:
        nonlocal last
        if stage == "unpack":
            last = 0
            print(f"  {name}: unpacking{(' ' + label) if label else ''}", flush=True)
            return
        if total <= 0:
            return
        if done >= total or done - last >= 4 * 1024 * 1024:
            last = done
            print(
                f"  {name}: {done / total * 100:5.1f}%  {_fmt_bytes(done)} / {_fmt_bytes(total)}",
                flush=True,
            )

    return report


def _install_names(names: list[str]) -> int:
    failed = 0
    for name in names:
        try:
            ensure(name, explicit=True, progress=_live_progress(name))
            print(f"✓ {name}", flush=True)
        except InstallError as e:
            print(f"✗ {e}", flush=True)
            failed += 1
    return failed



def _bundle_package_names() -> list[str]:
    names = [
        n
        for n in _lockfile().names()
        if not get_package(n).internal or n == "uv"
    ]
    if "python" not in names:
        names.append("python")
    return names


def _drop_unloadable_runtime_files(store_dir: Path) -> None:
    """Drop the x64 VC runtime that python-build-standalone ships beside ARM64 Python."""
    if current_target() != "win32-arm64":
        return
    facts = _facts()
    facts.reload()
    python = facts.get("python")
    if python and "entry" in python:
        (store_dir / python["entry"] / "vcruntime140_1.dll").unlink(missing_ok=True)


def cmd_install(args) -> int:
    names = args.names or [
        n for n in _lockfile().names() if not get_package(n).optional
    ]
    failed = _install_names(names)
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
            soft = package.optional or package.internal
            print(f"{'?' if soft else '✗'} {name}: {state}")
            bad += 0 if soft else 1
            continue
        entry = store.entry(facts.get(name)["entry"])
        reason = package.verify(entry, target)
        if reason:
            print(f"✗ {name}: installed but failed verification: {reason}")
            bad += 1
            continue
        print(f"✓ {name} {facts.get(name)['version']}")
    return 1 if bad else 0


def cmd_develop(args) -> int:
    """Install everything, sync the venv, then activate: spawn a subshell
    with every tool's env + the venv composed in (or --print eval-able
    exports for the current shell). The devshell equivalent of nix develop."""
    import os
    import subprocess

    from pm.ensure import env_for, sync_venv

    failed = _install_names(
        [
            n for n in _lockfile().names()
            if not get_package(n).optional
            and get_package(n).missing_reason(current_target()) is None
        ]
    )
    try:
        sync_venv(explicit=True)
        print("✓ venv")
    except InstallError as e:
        print(f"✗ {e}")
        failed += 1
    if failed:
        return 1

    from pm import paths

    env = env_for(*_lockfile().names(), base_env=dict(os.environ))
    venv_dir = paths.repo_root() / (
        ".venv" if (paths.repo_root() / ".venv").is_dir() else "venv"
    )
    win = current_target().startswith("win32")
    venv_bin = venv_dir / ("Scripts" if win else "bin")
    env["VIRTUAL_ENV"] = str(venv_dir)
    env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", "")
    env.pop("PYTHONHOME", None)

    if args.print_env:
        changed = {k: v for k, v in env.items() if os.environ.get(k) != v}
        for key, value in sorted(changed.items()):
            if win and os.environ.get("SHELL") is None:
                print(f'$env:{key} = "{value}"')
            else:
                escaped = value.replace("'", "'\\''")
                print(f"export {key}='{escaped}'")
        return 0

    shell = os.environ.get("SHELL") or os.environ.get("COMSPEC") or (
        "cmd.exe" if win else "/bin/sh"
    )
    print(f"pm develop: entering {shell} (exit to leave)")
    return subprocess.call([shell], env=env, cwd=paths.repo_root())


def cmd_gc(args) -> int:
    facts = _facts()
    store = _store()
    if not store.root.is_dir():
        return 0
    removed = 0
    with store.install_lock():
        facts.reload()
        keep = facts.entries_in_use()
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


def _run_live(cmd: list[str], *, cwd, env, timeout: int = 3600) -> tuple[int, str]:
    """Run cmd with its output streamed through our stdout — a long uv
    venv build must prove liveness in a piped (CI) log, not vanish until
    exit — while still capturing the tail for the failure message. A
    reader thread drains output so proc.wait(timeout) keeps the wall-clock
    kill the old subprocess.run(timeout=) had. Returns (returncode, last
    ~2k chars of combined output)."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1, errors="replace",
    )
    tail = ""
    lock = threading.Lock()

    def drain() -> None:
        nonlocal tail
        for line in proc.stdout:
            print(line, end="", flush=True)
            with lock:
                tail = (tail + line)[-2000:]

    thread = threading.Thread(target=drain, daemon=True)
    thread.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError(f"{cmd[0]} timed out after {timeout}s")
    thread.join()
    with lock:
        return code, tail


def cmd_bundle(args) -> int:
    """Stage a complete payload for THIS machine's target into --out:
    repo snapshot + store + facts (via the normal install path, redirected)
    + a relocatable venv built on the staged interpreter and synced from
    uv.lock. Built natively per (os, arch); there is no cross-target
    staging."""
    import os

    from pm import paths

    out = Path(args.out).resolve()
    store_dir = out / "tools"
    store_dir.mkdir(parents=True, exist_ok=True)
    # A manifest from a previous run would make this payload look sealed
    # and refuse its own staging; it is rewritten at the end.
    (out / "manifest.json").unlink(missing_ok=True)

    repo_dir = out / "hermes-agent"
    ref = args.ref or "HEAD"
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir.mkdir(parents=True)
    print(f"staging repo snapshot ({ref})…", flush=True)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", ref],
        cwd=paths.repo_root(), capture_output=True, timeout=600,
    )
    if archive.returncode != 0:
        print(f"✗ repo: git archive {ref} failed: {archive.stderr.decode()[-500:]}")
        return 1
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(repo_dir, filter="data")
    print(f"✓ repo ({ref})")

    os.environ["HERMES_RUNTIME_DIR"] = str(store_dir)
    paths._stamp.cache_clear()

    failed = 0
    names = _bundle_package_names()
    failed += _install_names(
        [n for n in names if get_package(n).missing_reason(current_target()) is None]
    )
    _drop_unloadable_runtime_files(store_dir)

    uv_bin, env = pm_uv()
    if uv_bin is None:
        print("✗ venv: uv did not stage")
        return 1

    python_fact = _facts().get("python")
    if python_fact is None:
        print("✗ venv: no staged interpreter to build on")
        return 1
    python_bin = get_package("python").binary(
        _store().entry(python_fact["entry"]), current_target()
    )

    # Build + sync INSIDE the staged repo: the editable project install
    # must point at the payload's own tree, not this checkout.
    venv_dir = out / "venv"
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    env["VIRTUAL_ENV"] = str(venv_dir)
    env.pop("UV_NO_CONFIG", None)
    if current_target().startswith("darwin"):
        # python-build-standalone bakes phantom toolchain paths (its build
        # dir's llvm-ar) into sysconfig; sdist builds then fail with
        # "No such file or directory: .../tools/llvm/bin/llvm-ar". Point
        # sdist builds at the machine's real toolchain.
        env.setdefault("AR", "/usr/bin/ar")
        env.setdefault("CC", "clang")
    for cmd in (
        [uv_bin, "venv", "--relocatable", "--python", str(python_bin), str(venv_dir)],
        [uv_bin, "sync", "--frozen", "--all-extras", "--active"],
    ):
        print(f"  venv: $ {' '.join(cmd)}", flush=True)
        code, tail = _run_live(cmd, cwd=repo_dir, env=env)
        if code != 0:
            print(f"✗ venv: {' '.join(cmd[1:3])} failed:\n{tail}")
            return 1
    print("✓ venv (relocatable, all extras, on the staged interpreter)")

    bad = _arch_guard(store_dir)
    for line in bad:
        print(f"✗ arch: {line}")
        failed += 1

    manifest = {
        "schema": 1,
        "target": current_target(),
        "ref": ref,
        "repo": "hermes-agent",
        "venv": "venv",
        "store": "tools",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"✓ manifest ({out / 'manifest.json'})")
    return 1 if failed else 0


def _arch_guard(store_dir: Path) -> list[str]:
    """Every staged binary must be built for this machine's target — a
    payload staged with a mismatched interpreter or PATH tool ships an
    artifact that cannot run. Reads facts, probes each entry binary."""
    from pm.lock import Facts
    from pm.package import machine_matches_binary

    facts = Facts(store_dir / "facts.json")
    problems = []
    target = current_target()
    for name in _lockfile().names():
        package = get_package(name)
        fact = facts.get(name)
        if fact is None or "entry" not in fact:
            continue
        binary = package.binary(store_dir / fact["entry"], target)
        if binary is None or not binary.is_file():
            continue
        verdict = machine_matches_binary(binary, target)
        # A package that declares this target as emulated (x64 binary run
        # under Windows ARM64 built-in emulation) is fine with the x64 PE.
        if verdict is False and target not in package.emulated_arch_targets:
            problems.append(f"{name}: {binary.name} is not a {target} binary")
    return problems


def main(argv=None) -> int:
    # Windows consoles default to cp1252; pm prints ✓/✗. Never let the
    # status glyphs crash the command reporting them. line_buffering:
    # pm output must stream live in a piped (CI) log, not sit in a block
    # buffer and flush only at exit.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace", line_buffering=True)
        except (AttributeError, OSError):
            pass
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

    p = sub.add_parser("develop", help="install + sync, then activate a devshell with the composed env")
    p.add_argument("--print", dest="print_env", action="store_true",
                   help="print eval-able exports instead of spawning a shell")
    p.set_defaults(func=cmd_develop)

    p = sub.add_parser("gc", help="remove store entries nothing references")
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser("bundle", help="stage a payload (repo+store+facts+relocatable venv) into --out")
    p.add_argument("--out", required=True)
    p.add_argument("--ref", help="git ref for the repo snapshot (default HEAD)")
    p.set_defaults(func=cmd_bundle)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
