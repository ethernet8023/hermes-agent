"""Every payload pin must accept the payload interpreter.

A package declares `Requires-Python`, and a resolver that ignores it produces
a lockfile the installer refuses. That is not hypothetical: uv treats a locked
pin's `Requires-Python` as advisory and installs anyway, while pip treats it
as binding and fails. The bundled payload staging is where uv-resolved
requirements meet real pip, so the disagreement surfaces there and nowhere
else — as a release-lane failure, on whichever target happens to stage the
offending extra.

    ERROR: Ignored the following versions that require a different python
           version: 1.3.1 Requires-Python >=3.8.6,<3.11
    ERROR: No matching distribution found for backports-strenum==1.3.1

This check cannot be a unit test, because the fact it needs is not local.
`Requires-Python` lives in neither the wheel filename nor uv.lock (which
records only the PROJECT's requires-python), so the only source is the index
itself: one PEP 691 request per distinct package. tests/tools/
test_payload_installability.py stays offline and covers what the lockfile
CAN answer — wheel tags and sdist presence. This covers the third fact.

Exit 0 when every pin accepts the interpreter, 1 otherwise, naming each
offender with its constraint and the targets it breaks.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SIMPLE_API = "https://pypi.org/simple/{name}/"
SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"

# One requirement line of a uv export, after continuations are joined.
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^ ;]+)\s*(?:;\s*(?P<marker>.+))?$")


def payload_python_version() -> str:
    """The interpreter the payload ships, from the pin table.

    The same source stage-agent-payloads.mjs reads, so this check and the
    build can never disagree about which interpreter is being audited.
    """
    pins = json.loads((REPO_ROOT / "installation" / "runtime-pins.json").read_text(encoding="utf-8-sig"))
    version = pins["tools"]["uv"]["python"]
    if not version:
        raise SystemExit("runtime-pins.json names no payload python version")
    return version


def marker_env(target: str, python_version: str) -> dict[str, str]:
    """A PEP 508 marker environment for *target* on the payload interpreter."""
    platform, _, arch = target.partition("-")
    major_minor = ".".join(python_version.split(".")[:2])
    return {
        "sys_platform": platform,
        "os_name": "nt" if platform == "win32" else "posix",
        "platform_system": {"linux": "Linux", "darwin": "Darwin", "win32": "Windows"}[platform],
        "platform_machine": {
            ("linux", "x64"): "x86_64", ("linux", "arm64"): "aarch64",
            ("darwin", "x64"): "x86_64", ("darwin", "arm64"): "arm64",
            ("win32", "x64"): "AMD64", ("win32", "arm64"): "ARM64",
        }[(platform, arch)],
        "python_version": major_minor,
        "python_full_version": python_version,
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
    }


def marker_admits(marker: str | None, env: dict[str, str]) -> bool:
    """Does a pin's marker hold in *env*? An unreadable marker counts as yes."""
    if not marker:
        return True
    from packaging.markers import Marker

    try:
        return bool(Marker(marker).evaluate(env))
    except Exception:
        return True


def export(extras: list[str]) -> list[tuple[str, str, str | None]]:
    """The staging script's own export, parsed into (name, version, marker)."""
    cmd = ["uv", "export", "--frozen", "--no-emit-project"]
    for extra in extras:
        cmd += ["--extra", extra]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise SystemExit(f"uv export failed:\n{proc.stderr}")

    pins: list[tuple[str, str, str | None]] = []
    for line in re.sub(r"\\\n", " ", proc.stdout).splitlines():
        line = line.split(" --hash")[0].strip()
        if not line or line.startswith(("#", "-")):
            continue
        m = _PIN.match(line)
        if m:
            pins.append((m.group("name"), m.group("version"), m.group("marker")))
    if not pins:
        raise SystemExit("uv export produced no pins")
    return pins


def requires_python(name: str, version: str) -> set[str | None] | None:
    """Every Requires-Python the index reports for this exact version.

    None when the index has no file for it at all, which is a different
    fault and is left to the offline installability test.
    """
    request = urllib.request.Request(
        SIMPLE_API.format(name=name), headers={"Accept": SIMPLE_ACCEPT}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{name}: cannot read the index: {exc}") from exc

    # Match the version segment exactly. A prefix test alone pairs
    # "1.2" with "1.2.8", and normalization means the filename may use
    # either separator.
    stems = {f"{name.replace('-', '_')}-{version}", f"{name.replace('_', '-')}-{version}"}
    specs: set[str | None] = set()
    for entry in data.get("files", []):
        filename = entry["filename"]
        for stem in stems:
            rest = filename[len(stem):] if filename.startswith(stem) else None
            if rest is not None and (rest.startswith(("-", ".")) or not rest):
                specs.add(entry.get("requires-python") or None)
                break
    return specs or None


def accepts(spec: str | None, python_version: str) -> bool:
    if not spec:
        return True
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    try:
        return Version(python_version) in SpecifierSet(spec)
    except Exception:
        # An unparseable specifier is the package's problem, not ours;
        # do not fail a release lane over it.
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--targets", default="",
        help="Comma-separated targets to audit. Default: every target.",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(REPO_ROOT))
    import tools.lazy_deps as ld
    from unittest.mock import patch

    python_version = payload_python_version()
    targets = [t for t in args.targets.split(",") if t] or list(ld.ALL_TARGETS)
    print(f"payload interpreter: cpython {python_version}")
    print(f"targets: {', '.join(targets)}\n")

    # The extras each target stages, asked exactly as the build asks.
    per_target: dict[str, list[tuple[str, str, str | None]]] = {}
    every_pin: set[tuple[str, str]] = set()
    for target in targets:
        with patch("installation.registry.current_target", return_value=target):
            extras = ld.bundle_extras()
        env = marker_env(target, python_version)
        pins = [p for p in export(extras) if marker_admits(p[2], env)]
        per_target[target] = pins
        every_pin |= {(name, version) for name, version, _ in pins}

    print(f"{len(every_pin)} distinct pins across {len(targets)} targets; reading the index\n")

    # One request per distinct (name, version), shared across targets.
    constraints: dict[tuple[str, str], set[str | None] | None] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        futures = {
            pool.submit(requires_python, name, version): (name, version)
            for name, version in sorted(every_pin)
        }
        for future in concurrent.futures.as_completed(futures):
            constraints[futures[future]] = future.result()

    failures: dict[tuple[str, str], tuple[set[str | None], list[str]]] = {}
    for target, pins in per_target.items():
        for name, version, _ in pins:
            specs = constraints.get((name, version))
            if specs is None:
                continue  # no files on the index: the offline test owns this
            if any(accepts(spec, python_version) for spec in specs):
                continue
            failures.setdefault((name, version), (specs, []))[1].append(target)

    if not failures:
        print(f"OK: every pin accepts cpython {python_version}")
        return 0

    print(f"FAIL: {len(failures)} pin(s) exclude cpython {python_version}\n")
    for (name, version), (specs, broken) in sorted(failures.items()):
        shown = ", ".join(sorted(s or "(none)" for s in specs))
        print(f"  {name}=={version}")
        print(f"      Requires-Python: {shown}")
        print(f"      breaks: {', '.join(sorted(broken))}")
    print(
        "\nuv installs these anyway; pip refuses them, so the bundled payload\n"
        "staging fails on the targets above. Add a [tool.uv]\n"
        "override-dependencies entry pinning a version that accepts the\n"
        "payload interpreter, then re-run `uv lock`."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
