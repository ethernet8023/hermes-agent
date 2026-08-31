#!/usr/bin/env python3
"""Container-side wheelhouse build: build the android build set from sdist
against THIS container's bionic python, retag, and run the offline
completeness + import gates. Invoked by termux_build.sh inside the
digest-pinned termux/termux-docker container.

Usage: build_wheels.py --resolved RESOLVED.txt --build-set BUILD_SET.txt \
           --wheelhouse DIR --retag RETAG_SCRIPT --platform-tag TAG

Everything here runs under the container's termux python (bionic): pip
builds sdists with clang/rust from $PREFIX, wheels land in the wheelhouse,
retagging stamps the PEP 738 android tag, and the --no-index gate proves the
FULL marker-admitted lock graph installs offline from the built bytes.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
import shutil
from pathlib import Path, PurePosixPath


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--resolved", required=True, help="tab-separated name/spec/marker file")
    p.add_argument("--build-set", required=True, help="names needing sdist builds (one per line)")
    p.add_argument("--wheelhouse", required=True)
    p.add_argument("--retag", required=True, help="path to retag_wheel.py")
    p.add_argument("--platform-tag", required=True)
    p.add_argument("--uv", required=True, help="path to the staged uv binary")
    return p.parse_args()


def parse_reqs(resolved: Path) -> list[tuple[str, str]]:
    """(name, spec) pairs for every non-empty resolved line."""
    out: list[tuple[str, str]] = []
    for line in resolved.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        out.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return out


def load_entries(resolved: Path) -> dict[str, str]:
    """name -> version spec ('==x.y.z') for the build loop's locked pins."""
    entries: dict[str, str] = {}
    for name, spec in parse_reqs(resolved):
        entries[name] = spec.strip()
    return entries


def write_reqs_file(resolved: Path, reqs: Path) -> None:
    """name==spec lines for a pip/uv -r file."""
    reqs.write_text(
        "\n".join(f"{name}{spec.strip()}" if spec.strip() else name
                  for name, spec in parse_reqs(resolved)) + "\n",
        encoding="utf-8",
    )


def safe_extract(archive: Path, dest: Path) -> Path:
    """Safe-extract a tarball, rejecting traversal/symlink/device members."""
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            path = PurePosixPath(member.name)
            parts = tuple(p for p in path.parts if p not in ("", "."))
            if path.is_absolute() or ".." in parts or not parts:
                raise RuntimeError(f"unsafe archive member path: {member.name!r}")
            target = dest.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise RuntimeError(f"unsupported archive member type: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                raise RuntimeError(f"cannot read archive member: {member.name}")
            with extracted, open(target, "wb") as dst:
                shutil.copyfileobj(extracted, dst)
            try:
                target.chmod(member.mode & 0o777)
            except OSError:
                pass
    roots = sorted(p for p in dest.iterdir() if p.is_dir())
    return roots[0] if roots else dest


PSUTIL_MARKER = 'LINUX = sys.platform.startswith("linux")'
PSUTIL_PATCH = 'LINUX = sys.platform.startswith(("linux", "android"))'


def patch_psutil(src_root: Path) -> None:
    common = src_root / "psutil" / "_common.py"
    if not common.is_file():
        return  # not a psutil sdist; nothing to patch
    content = common.read_text(encoding="utf-8-sig")
    if PSUTIL_MARKER not in content:
        raise RuntimeError("psutil android patch marker not found -- update the patch for the pinned psutil pin")
    common.write_text(content.replace(PSUTIL_MARKER, PSUTIL_PATCH), encoding="utf-8")


def build_wheels(build_set: list[str], specs: dict[str, str], wheelhouse: Path) -> None:
    for name in build_set:
        spec = specs.get(name, "")
        req = f"{name}{spec}" if spec else name
        if name in ("psutil", "uvloop"):
            print(f"==> building {name} (download + extract + pre-build fixups)")
            with tempfile.TemporaryDirectory(prefix=f"hermes-build-{name}-") as tmp:
                tmp = Path(tmp)
                sdist_dir = tmp / "sdist"
                sdist_dir.mkdir()
                subprocess.run(
                    [sys.executable, "-m", "pip", "download", "--no-deps", "--no-binary", ":all:",
                     "--no-build-isolation", "-d", str(sdist_dir), req],
                    check=True, cwd=tmp,
                )
                archives = list(sdist_dir.glob("*.tar.gz"))
                if len(archives) != 1:
                    raise RuntimeError(f"expected exactly one sdist archive for {name}, got {len(archives)}")
                src = safe_extract(archives[0], tmp / "src")
                patch_psutil(src)
                if name == "uvloop":
                    # uvloop vendors libuv at vendor/libuv/ WITH its own
                    # autogen.sh + generated configure; the build backend
                    # expects libuv CONFIGURED (build artifacts present)
                    # before setup.py runs it. We run configure ourselves:
                    # setup.py's own ['./configure'] invocation 127s on the
                    # rewritten shebang (subprocess execv semantics), but a
                    # completed configure leaves artifacts setup.py reuses.
                    libuv = src / "vendor" / "libuv"
                    if libuv.is_dir():
                        # bash-invoked: the sdist's autogen.sh may carry a
                        # non-exec mode (extraction preserves it). autogen
                        # also (re)writes ./configure WITHOUT the exec bit
                        # (setup.py then 127s) -- chmod after bootstrap.
                        proc = subprocess.run(["bash", "autogen.sh"], cwd=libuv,
                                              check=False, capture_output=True, text=True)
                        if proc.returncode == 0 and (libuv / "configure").is_file():
                            # Run configure under the container's own sh
                            # (its #!/bin/sh shebang can't exec here).
                            cfg_proc = subprocess.run(
                                [os.environ["PREFIX"] + "/bin/sh", "./configure"],
                                cwd=libuv, check=False, capture_output=True, text=True,
                            )
                            if cfg_proc.returncode != 0:
                                print(f"FIXUP FAILED (libuv configure) for {name}")
                                print("stdout:", cfg_proc.stdout[-1500:])
                                print("stderr:", cfg_proc.stderr[-1500:])
                                raise subprocess.CalledProcessError(cfg_proc.returncode, cfg_proc.args)
                        if proc.returncode != 0:
                            # autogen needs autoreconf when configure is
                            # stale; fall back to explicit bootstrap
                            proc2 = subprocess.run(["autoreconf", "-i"], cwd=libuv,
                                                   check=False, capture_output=True, text=True)
                            if proc2.returncode != 0:
                                print(f"FIXUP FAILED (libuv bootstrap) for {name}")
                                print("autogen stdout:", proc.stdout[-1200:])
                                print("autogen stderr:", proc.stderr[-1200:])
                                print("autoreconf stderr:", proc2.stderr[-1200:])
                                raise subprocess.CalledProcessError(proc2.returncode, proc2.args)
                proc = subprocess.run(
                    [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                     "-w", str(wheelhouse), str(src)],
                    check=False, cwd=tmp, capture_output=True, text=True,
                )
                if proc.returncode != 0:
                    print(f"BUILD FAILED: {name}")
                    print("stdout:", proc.stdout[-2000:])
                    print("stderr:", proc.stderr[-2000:])
                    raise subprocess.CalledProcessError(proc.returncode, proc.args)
        else:
            # Build isolation LEFT ON: --no-build-isolation requires every
            # sdist's declared backend (pdm, hatchling, maturin...) to be
            # pre-installed, and the lock graph uses more backends than the
            # pinned toolchain covers. The invariant is the USER machine
            # never resolves/compiles -- the build container may fetch.
            print(f"==> building {name} (direct pip wheel, isolated backend)")
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", "--no-deps",
                 "--no-binary", ":all:", "-w", str(wheelhouse), req],
                check=False, capture_output=True, text=True,
            )
            if proc.returncode != 0:
                print(f"BUILD FAILED: {name}")
                print("stdout:", proc.stdout[-2000:])
                print("stderr:", proc.stderr[-2000:])
                raise subprocess.CalledProcessError(proc.returncode, proc.args)


TARGET_ENV = {
    "implementation_name": "cpython",
    "implementation_version": "3.11.15",
    "os_name": "posix",
    "platform_machine": "aarch64",
    "platform_release": "",
    "platform_system": "Linux",
    "platform_version": "",
    "python_full_version": "3.11.15",
    "python_version": "3.11",
    "sys_platform": "linux",
}

# Mirrors the documented misses in termux_build.sh's probe (single
# source is ideal; duplicated until the probe emits a sidecar file).
BUILD_MISSES = {"nemo-relay"}


def _marker_admits(marker: str) -> bool:
    if not marker:
        return True
    from packaging.markers import Marker
    try:
        return Marker(marker).evaluate(TARGET_ENV)
    except Exception:
        return True


def _parse_full(resolved: Path) -> list[tuple[str, str, str]]:
    """(name, spec, marker) triples from the tab-separated resolved file."""
    out: list[tuple[str, str, str]] = []
    for line in resolved.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        out.append((parts[0], parts[1] if len(parts) > 1 else "",
                    parts[2] if len(parts) > 2 else ""))
    return out


def fetch_pure_wheels(build_set: list[str], specs: dict[str, str], wheelhouse: Path, resolved: Path) -> None:
    """Fetch every APPLICABLE dep's py3-none-any wheel into the wheelhouse.

    The offline completeness gate installs the FULL marker-admitted graph
    from --find-links wheelhouse under --no-index; the natives were built
    here, but the pure wheels live only on PyPI. Fetch them (the build
    container may resolve; the USER machine never does). Wheels already
    present (built natives) are skipped by pip download naturally.
    Marker-excluded deps (pywin32, winrt-*) and documented build misses
    (nemo-relay) are skipped -- the gate's reqs carry the same exclusions
    via the venv's own marker evaluation... which they do NOT: write_reqs_file
    writes the full list and uv evaluates markers from the RUNNING platform.
    The gate reqs keep all lines; pip/uv on bionic skips the win32 ones.
    """
    reqs = []
    for name, spec, marker in _parse_full(resolved):
        if name in build_set or name in BUILD_MISSES:
            continue
        if not _marker_admits(marker):
            continue
        reqs.append(f"{name}{spec.strip()}" if spec.strip() else name)
    if not reqs:
        return
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "download", "--no-deps",
         "--only-binary", ":all:", "-d", str(wheelhouse), *reqs],
        check=False, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print("PURE-WHEEL FETCH FAILED")
        print("stdout:", proc.stdout[-1500:])
        print("stderr:", proc.stderr[-1500:])
        raise subprocess.CalledProcessError(proc.returncode, proc.args)
    fetched = len(list(wheelhouse.glob("*.whl")))
    print(f"  pure wheels fetched; wheelhouse now holds {fetched} wheels")


def retag_all(wheelhouse: Path, retag_script: Path, platform_tag: str) -> None:
    wheels = sorted(wheelhouse.glob("*.whl"))
    if not wheels:
        raise RuntimeError(f"no wheels built into {wheelhouse}")
    # maturin-built wheels (cryptography, pydantic-core, jiter, watchfiles)
    # emerge ANDROID-tagged already (ANDROID_API_LEVEL drives the build);
    # only the setuptools/distutils-built ones carry linux_aarch64 tags and
    # need the retag. Retagging an android wheel again is a hard error.
    linux_tagged = [w for w in wheels if "linux" in w.name and "none-any" not in w.name]
    android_tagged = [w for w in wheels if "android" in w.name]
    if linux_tagged:
        subprocess.run(
            [sys.executable, str(retag_script), "--platform-tag", platform_tag, *linux_tagged],
            check=True,
        )
    retagged = list(wheelhouse.glob(f"*{platform_tag}*.whl"))
    total_android = len(retagged)
    if total_android == 0:
        raise RuntimeError(f"no wheels retagged to {platform_tag}")
    print(f"  retagged {len(linux_tagged)} linux-tagged wheels; {len(android_tagged)} were android-tagged at build; {total_android} android wheels total")


def completeness_gate(resolved: Path, wheelhouse: Path) -> None:
    """Offline install of every marker-admitted dep into a clean venv.

    The container's own bionic python evaluates the markers exactly as the
    phone will (sys_platform/platform_machine are real here), so deps the
    android target excludes (pywin32, winrt-*) are skipped by pip itself --
    no reimplementation. --no-index makes completeness a build-time
    invariant: a dep missing from the wheelhouse fails loudly.
    """
    with tempfile.TemporaryDirectory(prefix="hermes-wheelhouse-gate-") as tmp:
        tmp = Path(tmp)
        venv = tmp / "venv"
        subprocess.run(
            [UV, "venv", "--python", sys.executable, str(venv)],
            check=True,
        )
        vp = venv / "bin" / "python"
        reqs = tmp / "reqs.txt"
        write_reqs_file(resolved, reqs)
        subprocess.run(
            [UV, "pip", "install", "--python", str(vp),
             "--only-binary", ":all:", "--no-index",
             "--find-links", str(wheelhouse), "-r", str(reqs)],
            check=True,
        )
        subprocess.run([UV, "pip", "check", "--python", str(vp)], check=True)
    print("  completeness gate: offline install of the marker-admitted graph OK")


def import_gate(resolved: Path, wheelhouse: Path) -> None:
    """Every package in the offline-installed venv must import. Runs pip
    install into a scratch venv again (cheap: wheelhouse is local)."""
    with tempfile.TemporaryDirectory(prefix="hermes-import-gate-") as tmp:
        tmp = Path(tmp)
        venv = tmp / "venv"
        subprocess.run([UV, "venv", "--python", sys.executable, str(venv)], check=True)
        vp = venv / "bin" / "python"
        reqs = tmp / "reqs.txt"
        write_reqs_file(resolved, reqs)
        subprocess.run(
            [UV, "pip", "install", "--python", str(vp),
             "--only-binary", ":all:", "--no-index",
             "--find-links", str(wheelhouse), "-r", str(reqs)],
            check=True,
        )
        import importlib.metadata as md
        dists = md.distributions()
        names = []
        for d in dists:
            n = (d.metadata["Name"] or "").replace("-", "_")
            if n:
                names.append(n)
        script = ",".join(sorted(set(names)))
        subprocess.run(
            [str(vp), "-c",
             "import importlib, sys\n"
             "for name in sys.argv[1].split(','):\n"
             "    importlib.import_module(name)\n"
             "    print('  imported', name)\n",
             script],
            check=True,
        )


UV = ""


def main() -> int:
    global UV
    args = parse_args()
    UV = str(Path(args.uv))
    resolved = Path(args.resolved)
    build_set = [l.strip() for l in Path(args.build_set).read_text(encoding="utf-8").splitlines() if l.strip()]
    wheelhouse = Path(args.wheelhouse)
    wheelhouse.mkdir(parents=True, exist_ok=True)
    specs = load_entries(resolved)

    build_wheels(build_set, specs, wheelhouse)
    fetch_pure_wheels(build_set, specs, wheelhouse, Path(args.resolved))
    retag_all(wheelhouse, Path(args.retag), args.platform_tag)
    completeness_gate(resolved, wheelhouse)
    import_gate(resolved, wheelhouse)
    print(f"wheelhouse complete: {len(list(wheelhouse.glob('*.whl')))} wheels in {wheelhouse}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
