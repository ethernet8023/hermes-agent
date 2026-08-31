"""Tests for scripts/termux/pins.json — the termux .deb pin table.

Stdlib + pytest only. Validates structure, key presence, and version formats.
The actual pin *values* are expected to be bumped by review; these tests pin
the shape, not the dates.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PINS_PATH = REPO_ROOT / "scripts" / "termux" / "pins.json"

REQUIRED_TOP_KEYS = [
    "schemaVersion",
    "termuxPackages",
    "python",
    "node",
    "termuxDocker",
    "toolchain",
]

SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
PY_SEMVER_RE = re.compile(r"^3\.\d+\.\d+$")


@pytest.fixture(scope="module")
def pins() -> dict:
    assert PINS_PATH.is_file(), f"missing pin table: {PINS_PATH}"
    return json.loads(PINS_PATH.read_text(encoding="ascii"))


def test_pins_json_is_ascii(pins_path_tmp=None):
    raw = PINS_PATH.read_bytes()
    raw.decode("ascii")


def test_required_top_level_keys(pins):
    for key in REQUIRED_TOP_KEYS:
        assert key in pins, f"pins.json missing required key: {key}"


def test_schema_version_is_int_1(pins):
    assert isinstance(pins["schemaVersion"], int)
    assert pins["schemaVersion"] == 1


def test_termux_packages_pin(pins):
    tp = pins["termuxPackages"]
    assert re.fullmatch(r"[0-9a-f]{40}", tp["commit"]), "commit must be a full 40-hex sha"
    assert tp["repo"].startswith("https://github.com/termux/termux-packages")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", tp["pinnedOn"])


def test_python_version_is_cpython_semver(pins):
    assert PY_SEMVER_RE.fullmatch(pins["python"]["version"])
    assert pins["python"]["recipe"] == "packages/python"


def test_node_version_is_semver(pins):
    assert SEMVER_RE.fullmatch(pins["node"]["version"])



def test_termux_docker_digest(pins):
    td = pins["termuxDocker"]
    assert td["image"] == "docker.io/termux/termux-docker"
    assert SHA256_DIGEST_RE.fullmatch(td["digest"]), "digest must be sha256:<64 hex>"
    assert td["tag"] == "aarch64"
    assert td["architecture"] == "arm64"


def test_toolchain_pins_are_semver(pins):
    tc = pins["toolchain"]
    for pkg in ("setuptools", "cython", "pybind11", "maturin"):
        assert pkg in tc, f"toolchain missing pin: {pkg}"
        assert SEMVER_RE.fullmatch(tc[pkg]), f"{pkg} pin {tc[pkg]!r} is not X.Y.Z"


def test_python_semver_regex_is_consistent_with_pin(pins):
    # The python pin is a plain semver; the deb pin derives from it.
    assert PY_SEMVER_RE.fullmatch(pins["python"]["version"])



def test_wheel_platform_tag_is_valid_android_tag(pins):
    wheel = pins["wheel"]
    assert re.fullmatch(r"android_\d+_arm64_v8a", wheel["platformTag"]), (
        f"wheel.platformTag {wheel['platformTag']!r} is not a PEP 738 android tag"
    )


def test_python_abi_derivation_matches_python_pin(pins):
    # The build derives index.json's pythonAbi as cp<major><minor> from
    # python.version; pin the derivation contract here so a change to the
    # version format or the derivation is caught on both ends.
    pv = pins["python"]["version"].split(".")
    assert re.fullmatch(rf"cp{re.escape(pv[0])}{re.escape(pv[1])}", f"cp{pv[0]}{pv[1]}")


def test_termux_deb_pins_exact():
    """Contract: python/node are pinned as EXACT termux .debs — url + sha256
    + debVersion must all be present and shaped, and the sha256 in pins.json
    must be the digest of the exact URL's basename-matching artifact."""
    pins = json.loads(PINS_PATH.read_text(encoding="utf-8"))
    for key, want_pkg, want_ver in (("python", "python", "3.14.6"), ("node", "nodejs-lts", "24.18.0")):
        entry = pins[key]
        assert entry["source"] == "termux-deb"
        assert entry["version"] == want_ver
        assert entry["debVersion"].startswith(want_ver + "-"), entry["debVersion"]
        url = entry["debUrl"]
        assert url.startswith("https://packages.termux.dev/apt/termux-main/pool/"), url
        assert url.endswith("_aarch64.deb"), url
        assert want_pkg in url, url
        sha = entry["debSha256"]
        assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha), sha
        assert isinstance(entry["depends"], list) and entry["depends"], key


def test_termux_deb_pins_match_semver_pins():
    """Invariant: the debVersion in pins.json must agree with the plain
    version pin (debVersion is '3.14.6-1' when version is '3.14.6')."""
    pins = json.loads(PINS_PATH.read_text(encoding="utf-8"))
    for key in ("python", "node"):
        base = pins[key]["version"]
        assert pins[key]["debVersion"] == base + "-1", (key, pins[key]["debVersion"])
