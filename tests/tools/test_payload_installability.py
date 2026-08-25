"""Every payload pin must be installable on every target it ships to.

The desktop payload installs `uv export --frozen` output with pip, running
natively on each target's own runner. pip needs one of two things for each
pin: a published wheel whose tags fit the target, or an sdist it can compile
there. A pin with neither cannot install, and the build fails on that lane
only — which is how a broken target reaches a release lane unnoticed.

`tools/lazy_deps.py` refuses a feature whose packages cannot exist on a target
at all (:data:`UNAVAILABLE`). This module holds that verdict to the real pins:
an UNAVAILABLE gate must correspond to a real gap, and a target with no gate
must have no gap.

The pins come from `uv export`, the SAME command the staging script runs to
write requirements-payload.txt (stage-agent-payloads.mjs). Asking uv is the
point: it is the only thing that reads uv.lock the way the build does, and
it applies `[tool.uv] override-dependencies`, resolves a package the lock
splits across versions (scipy 1.17.1 below python 3.12, 1.18.0 above), and
emits the environment marker each pin carries. Re-deriving that closure by
walking uv.lock means writing a second resolver that agrees with uv only
until it does not — three separate bugs during this module's own
development came from exactly that, each one a confident wrong answer.

uv.lock is still read, for one thing uv does not put in the export: the
wheel FILENAMES, whose tags say which targets a published wheel fits. That
is a flat (name, version) -> filenames lookup, not a graph walk.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from packaging.markers import Marker
from packaging.utils import canonicalize_name

import tools.lazy_deps as ld


def at(target: str):
    """Run a block as if this host were ``target``.

    current_target() is the single seam every gate reads, so patching it
    simulates a build target honestly — no sys.platform faking, and every
    target is covered from whichever host runs the suite.
    """
    return patch("installation.registry.current_target", return_value=target)


REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK = REPO_ROOT / "uv.lock"

#: The payload interpreter. installation/runtime-pins.json pins the patch;
#: only the minor decides wheel tag compatibility and marker evaluation.
PY_MINOR = 11

#: Wheel platform tags each target can install, as regexes over one tag.
#: A wheel carries `.`-joined tags and installs when ANY of them fits.
TARGET_PLATFORM_TAGS: dict[str, tuple[str, ...]] = {
    "linux-x64": (r"manylinux.*_x86_64", r"musllinux.*_x86_64", r"linux_x86_64"),
    "linux-arm64": (r"manylinux.*_aarch64", r"musllinux.*_aarch64", r"linux_aarch64"),
    "darwin-x64": (r"macosx_.*_x86_64", r"macosx_.*_universal2", r"macosx_.*_intel"),
    "darwin-arm64": (r"macosx_.*_arm64", r"macosx_.*_universal2"),
    "win32-x64": (r"win_amd64", r"win32"),
    "win32-arm64": (r"win_arm64",),
}

#: PEP 508 marker environment per target, for the markers uv writes on a
#: platform-specific pin. Only the keys uv actually emits are set.
TARGET_MARKER_ENV: dict[str, dict[str, str]] = {
    "linux-x64": {"sys_platform": "linux", "os_name": "posix", "platform_system": "Linux", "platform_machine": "x86_64"},
    "linux-arm64": {"sys_platform": "linux", "os_name": "posix", "platform_system": "Linux", "platform_machine": "aarch64"},
    "darwin-x64": {"sys_platform": "darwin", "os_name": "posix", "platform_system": "Darwin", "platform_machine": "x86_64"},
    "darwin-arm64": {"sys_platform": "darwin", "os_name": "posix", "platform_system": "Darwin", "platform_machine": "arm64"},
    "win32-x64": {"sys_platform": "win32", "os_name": "nt", "platform_system": "Windows", "platform_machine": "AMD64"},
    "win32-arm64": {"sys_platform": "win32", "os_name": "nt", "platform_system": "Windows", "platform_machine": "ARM64"},
}

# A wheel filename: name-version(-build)?-pytag-abitag-platformtag.whl
_WHEEL_RE = re.compile(
    r"^(?P<name>.+?)-(?P<version>[^-]+?)(?:-\d[^-]*)?"
    r"-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl$"
)

# One requirement line of a uv export, after line continuations are joined.
_PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^ ;]+)\s*(?:;\s*(?P<marker>.+))?$")


def _uv() -> str:
    """The uv the build would use, or skip.

    uv IS the subject here: this module checks the export the staging
    script produces. Faking it would test the fake.
    """
    found = shutil.which("uv")
    if found is None:
        pytest.skip("uv is not on PATH (run under the project devshell)")
    return found


def _python_tag_fits(py_tag: str, abi_tag: str) -> bool:
    """Does a wheel's python/abi tag admit the payload interpreter?"""
    for tag in py_tag.split("."):
        if tag in ("py2", "py3"):
            return True
        m = re.fullmatch(r"(?:cp|py)3(\d*)", tag)
        if m is None:
            continue
        minor = int(m.group(1) or 0)
        if minor in (0, PY_MINOR):
            return True
        # An abi3 wheel built for an older minor stays importable later.
        if abi_tag.startswith("abi3") and minor <= PY_MINOR:
            return True
    return False


def _platform_tag_fits(platform_tag: str, patterns: tuple[str, ...]) -> bool:
    for tag in platform_tag.split("."):
        if tag == "any":
            return True
        if any(re.fullmatch(p, tag) for p in patterns):
            return True
    return False


def _wheel_fits(filename: str, target: str) -> bool:
    m = _WHEEL_RE.match(filename)
    if m is None:
        return False
    return _python_tag_fits(m.group("py"), m.group("abi")) and _platform_tag_fits(
        m.group("platform"), TARGET_PLATFORM_TAGS[target]
    )


def _marker_admits(marker: str | None, target: str) -> bool:
    """Does a pin's environment marker hold on *target*?

    An unparseable marker counts as present. A pin this check cannot read
    must not silently leave the audit.
    """
    if not marker:
        return True
    env = dict(TARGET_MARKER_ENV[target])
    env["python_version"] = f"3.{PY_MINOR}"
    env["python_full_version"] = f"3.{PY_MINOR}.0"
    env["implementation_name"] = "cpython"
    env["platform_python_implementation"] = "CPython"
    try:
        return bool(Marker(marker).evaluate(env))
    except Exception:
        return True


def _export(extras: tuple[str, ...]) -> list[tuple[str, str, str | None]]:
    """Run the staging script's own export and parse it.

    `--frozen` uses uv.lock as committed and never re-resolves, and
    `--offline` keeps the suite from reaching the network, so the answer
    describes exactly the versions this commit ships.
    """
    cmd = [_uv(), "export", "--frozen", "--offline", "--no-emit-project"]
    for extra in extras:
        cmd += ["--extra", extra]
    r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"uv export failed:\n{r.stderr}"

    pins: list[tuple[str, str, str | None]] = []
    for line in re.sub(r"\\\n", " ", r.stdout).splitlines():
        line = line.split(" --hash")[0].strip()
        if not line or line.startswith(("#", "-")):
            continue
        m = _PIN_RE.match(line)
        if m:
            pins.append((canonicalize_name(m.group("name")), m.group("version"), m.group("marker")))
    assert pins, "uv export produced no pins"
    return pins


@pytest.fixture(scope="module")
def wheels() -> dict[tuple[str, str], list[str]]:
    """Published wheel filenames per (canonical name, version).

    Keyed on the pair because the lock holds several versions of one
    package when a marker splits it. uv resolves which one applies; this
    only answers "what did that version publish?".
    """
    data = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    return {
        (canonicalize_name(p["name"]), p["version"]): [
            w["url"].rsplit("/", 1)[-1] for w in p.get("wheels", [])
        ]
        for p in data["package"]
    }


@pytest.fixture(scope="module")
def sdists() -> set[tuple[str, str]]:
    data = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    return {
        (canonicalize_name(p["name"]), p["version"]) for p in data["package"] if "sdist" in p
    }


@pytest.fixture(scope="module")
def payload_pins() -> dict[str, list[tuple[str, str, str | None]]]:
    """The pins each target's artifact installs, per target.

    The extras come from ``bundle_extras()`` AS THAT TARGET, which is the
    same probe the staging script runs on the target runner, so a backend
    the artifact drops here is not exported here either.
    """
    out: dict[str, list[tuple[str, str, str | None]]] = {}
    for target in ld.ALL_TARGETS:
        with at(target):
            extras = tuple(ld.bundle_extras())
        out[target] = [
            (name, version, marker)
            for name, version, marker in _export(extras)
            if _marker_admits(marker, target)
        ]
    return out


class TestPayloadInstallability:
    """The exported payload requirements, against every release target."""

    def test_the_target_tables_cover_every_target(self) -> None:
        assert set(TARGET_PLATFORM_TAGS) == set(ld.ALL_TARGETS)
        assert set(TARGET_MARKER_ENV) == set(ld.ALL_TARGETS)

    def test_every_pin_installs_by_wheel_or_by_sdist(
        self, payload_pins: dict, wheels: dict, sdists: set
    ) -> None:
        # The invariant the build depends on. A pin with no fitting wheel
        # and no sdist stops the payload install on that target, and pip
        # reports it as a version error ("from versions: …" lists what
        # survived filtering, not what the index holds), so the real cause
        # is easy to misread. Catch it here, against the real export.
        broken: list[str] = []
        for target, pins in payload_pins.items():
            for name, version, _ in pins:
                fits = any(_wheel_fits(f, target) for f in wheels.get((name, version), []))
                if not fits and (name, version) not in sdists:
                    broken.append(f"{target}: {name}=={version} has no wheel and no sdist")
        assert not broken, "\n".join(broken)

    def test_a_source_build_is_always_a_real_wheel_gap(
        self, payload_pins: dict, wheels: dict, sdists: set
    ) -> None:
        # pip compiles a pin only when no published wheel fits. Nothing
        # names those packages any more, so this proves each compile is a
        # genuine gap rather than a flag forcing a needless one.
        for target, pins in payload_pins.items():
            for name, version, _ in pins:
                if any(_wheel_fits(f, target) for f in wheels.get((name, version), [])):
                    continue
                assert (name, version) in sdists, (
                    f"{target}: {name}=={version} would compile with no sdist"
                )

    def test_an_unavailable_gate_names_a_real_gap(
        self, wheels: dict
    ) -> None:
        # An UNAVAILABLE gate claims the feature cannot work on a target for
        # anyone. The half of that claim this module can VERIFY is the
        # index-derived one: at least one pin has no wheel whose tags fit the
        # target, so installing there is not a matter of downloading a file.
        #
        # It deliberately does not also demand "and no sdist exists". Two
        # shapes both earn the verdict and only one has that property:
        #
        #   no wheel, no sdist      nothing to install at all (ctranslate2 and
        #                           onnxruntime, behind stt.faster_whisper).
        #   no wheel, dead sdist    a source archive that cannot produce a
        #                           wheel on the target (grpcio behind mem0 on
        #                           win32-arm64: its setup.py passes /std:c++17
        #                           and /std:c11 together and relies on a
        #                           monkeypatch of Compiler.spawn to strip the
        #                           wrong one per file, but setuptools now
        #                           calls Compiler.call, so cl fails D8016).
        #
        # Whether an sdist builds is not a fact about the index and cannot be
        # read off one — it is learned from a build and recorded in the gate's
        # own explainer, next to the gate. What stays checkable here is the
        # staleness property this test exists for: the day upstream publishes
        # a fitting wheel, every pin fits, this fails, and the gate comes out.
        stale: list[str] = []
        for feature in sorted(ld.LAZY_DEPS):
            extra = ld.feature_extra(feature)
            if not ld.extra_specs(extra):
                continue
            for target in ld.ALL_TARGETS:
                with at(target):
                    try:
                        ld.check_supported(feature)
                    except ld.UnsupportedFeature:
                        pass
                    else:
                        continue  # this target is not gated UNAVAILABLE
                gap = False
                for name, version, marker in _export((extra,)):
                    if not _marker_admits(marker, target):
                        continue
                    if not any(_wheel_fits(f, target) for f in wheels.get((name, version), [])):
                        gap = True
                        break
                if not gap:
                    stale.append(
                        f"{target}: {feature} is UNAVAILABLE, but every pin it "
                        f"needs has a wheel that fits — the gate is stale"
                    )
        assert not stale, "\n".join(stale)
