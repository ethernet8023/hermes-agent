#!/usr/bin/env bash
# Android/Termux wheelhouse builder -- the ONE entry point (Task 2 of
# .hermes/plans/2026-08-31_termux-deb.md). Runs inside the digest-pinned
# termux/termux-docker container on a native aarch64 runner; CI and local
# invoke it identically. No opt-out flags: a skipped step is a different
# artifact.
#
# Inputs (all required):
#   --repo <dir>      hermes-agent checkout to build from (must contain the tag)
#   --tag <tag>       immutable release tag (vX.Y.Z or vX.Y.Z-nightly.<ts>)
#   --out <dir>       output dir (wheelhouse/ + index.json + SHA256SUMS land here)
#
# Optionally exported by CI: HERMES_BUILD_INDEX_URL (internal uv index),
# GH_TOKEN (for gh release view), nothing else. The script is pure
# coreutils + git + curl + python3 + uv; it never apt-installs.

set -Eeuo pipefail

REPO=""
TAG=""
OUT=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo) REPO="${2:?}"; shift 2 ;;
        --tag) TAG="${2:?}"; shift 2 ;;
        --out) OUT="${2:?}"; shift 2 ;;
        *) printf 'usage: termux_build.sh --repo <dir> --tag <tag> --out <dir>\n' >&2; exit 2 ;;
    esac
done
[ -n "$REPO" ] && [ -n "$TAG" ] && [ -n "$OUT" ] || {
    printf 'usage: termux_build.sh --repo <dir> --tag <tag> --out <dir>\n' >&2; exit 2; }

log() { printf '\n==> %s\n' "$*"; }
fail() { printf 'termux_build: FAILED: %s\n' "$*" >&2; exit 1; }

# [a] Environment gate -- the build host lies. Refuse anything that is not
# native aarch64; a wrong-arch wheelhouse poisons every downstream install.
for tool in uv git curl python3; do
    command -v "$tool" >/dev/null 2>&1         || fail "missing tool: $tool (CI must provision the pinned toolchain before running this script)"
done

ARCH="$(uname -m)"
case "$ARCH" in
    aarch64|arm64) ;;
    *) fail "refusing to build on non-aarch64 host (uname -m: $ARCH). Run inside the pinned termux-docker container on an arm64 runner." ;;
esac

# [b] FIRST: refuse mutable releases. No building before the tag is proven
# to exist on the origin remote. An untagged wheelhouse has nothing to pin
# its lock to.
log "Verifying release tag $TAG exists on origin"
git -C "$REPO" ls-remote --exit-code --tags origin "$TAG" >/dev/null \
    || fail "tag $TAG not found on origin; refusing to build a mutable release"
if command -v gh >/dev/null 2>&1; then
    # Optional cross-check: the release must exist too (CI cuts the release
    # before building). Absent gh -> tag-exists check above already gated.
    gh release view "$TAG" --repo "$(git -C "$REPO" remote get-url origin | sed -e 's#.*github.com[:/]##' -e 's#\.git$##')" >/dev/null 2>&1 \
        || fail "release $TAG not found; refusing to build before the release exists"
fi

# Serial builds only -- parallel Rust/C builds OOM arm runners.
export CARGO_BUILD_JOBS=1
export MAKEFLAGS=-j1
export UV_CONCURRENT_BUILDS=1

REPO_ABS="$(cd "$REPO" && pwd)"
OUT_ABS="$(mkdir -p "$OUT" && cd "$OUT" && pwd)"
WORK="$OUT_ABS/.work"
WHEELHOUSE="$OUT_ABS/wheelhouse"
rm -rf "$WORK" "$WHEELHOUSE"
mkdir -p "$WORK" "$WHEELHOUSE"

# [c] Stage the tag as a gitless tree. Ship no .git (skill principle 1);
# the tree carries install-stamp.json for provenance instead.
log "Archiving $TAG into $WORK/tree"
git -C "$REPO_ABS" archive --format=tar "$TAG" | tar -xf - -C "$WORK/tree" 2>/dev/null || {
    mkdir -p "$WORK/tree"
    git -C "$REPO_ABS" archive --format=tar -o "$WORK/tree.tar" "$TAG"
    tar -xf "$WORK/tree.tar" -C "$WORK/tree"
    rm -f "$WORK/tree.tar"
}
[ -f "$WORK/tree/pyproject.toml" ] || fail "archived tag tree has no pyproject.toml -- bad tag?"

# [d] Resolve the real graph from the tag's own lock. The build set is
# derived: resolved minus installable-PyPI-wheel. NEVER a hand list.
log "Resolving dependency graph from the tag's uv.lock"
( cd "$WORK/tree" && uv export --frozen --no-emit-project -o "$WORK/req.txt" ) \
    || fail "uv export failed (frozen lock at $TAG)"
RESOLVED="$WORK/resolved.txt"
python3 - "$WORK/req.txt" "$RESOLVED" <<'PYEOF' || fail "failed to normalize requirements"
import re, sys
out = []
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    line = re.sub(r"\s*;.*$", "", line)  # drop environment markers for the probe
    line = re.sub(r"\s*--.*$", "", line)  # drop option lines entirely below
    if line.startswith("--") or not line:
        continue
    m = re.match(r"^([A-Za-z0-9._-]+)(\[[^]]*\])?([=<>~!^].*)?$", line)
    if m:
        out.append((m.group(1), m.group(3) or ""))
open(sys.argv[2], "w", encoding="utf-8").write(
    "\n".join(f"{name} { spec}" for name, spec in out) + "\n"
)
PYEOF
[ -s "$RESOLVED" ] || fail "resolved dependency list is empty"

# PyPI wheel-coverage probe: build set = resolved - (has an installable
# wheel). Pure-python deps with universal wheels skip the build entirely.
log "Probing PyPI wheel coverage"
BUILD_SET="$WORK/build_set.txt"
python3 - "$RESOLVED" "$BUILD_SET" <<'PYEOF' || fail "PyPI wheel-coverage probe failed"
import json, re, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor

resolved = [l.split(None, 1) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]

def locked_version(spec: str) -> str | None:
    """Extract the pinned version from an equality spec like '==1.2.3'."""
    m = re.search(r"==\s*([A-Za-z0-9._+!-]+)", spec or "")
    return m.group(1) if m else None

def probe(item: tuple[str, str]) -> tuple[str, bool, str | None]:
    name, spec = item
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=30) as r:
            d = json.load(r)
        # Probe the LOCKED version, not d['info']['version'] (latest):
        # the wheelhouse must cover exactly what the lock resolves to.
        locked = locked_version(spec) or d["info"]["version"]
        files = d["releases"].get(locked, [])
        return name, any(f["filename"].endswith(".whl") for f in files), None
    except Exception as exc:  # noqa: BLE001 -- a probe miss means BUILD it
        return name, False, str(exc)

with ThreadPoolExecutor(max_workers=10) as ex:
    results = list(ex.map(probe, resolved))

needs_build = []
for name, has_wheel, err in results:
    if err is not None:
        print(f"  probe miss {name}: {err} -> building from sdist", file=sys.stderr)
    if not has_wheel:
        needs_build.append(name)
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(needs_build) + "\n")
print(f"  {len(needs_build)} of {len(resolved)} packages need sdist builds")
PYEOF

# Pins single-sourcing: everything below reads the tag's own pins.json --
# platformTag (retag + index), pythonAbi (derived), toolchain build pins.
PINS="$WORK/tree/scripts/termux/pins.json"
[ -f "$PINS" ] || fail "pins.json missing from the archived tag tree"
PLATFORM_TAG="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["wheel"]["platformTag"])' "$PINS")" \
    || fail "failed to read wheel.platformTag from pins.json"
[ -n "$PLATFORM_TAG" ] || fail "pins.json wheel.platformTag is empty"

# [f] Enforce the toolchain pins from pins.json -- without this the pin table
# is a lie: --no-build-isolation builds use whatever setuptools/cython/...
# the container happens to have. Install the pinned versions up front.
log "Enforcing toolchain pins from pins.json"
python3 - "$PINS" <<'PYEOF' || fail "toolchain pin enforcement failed"
import json, subprocess, sys
tc = json.load(open(sys.argv[1], encoding="utf-8"))["toolchain"]
order = ["setuptools", "cython", "pybind11", "maturin"]
pkgs = [f"{name}=={tc[name]}" for name in order if name in tc]
print(f"  installing: {' '.join(pkgs)}")
subprocess.run([sys.executable, "-m", "pip", "install", *pkgs], check=True)
PYEOF

# [e]-[h] Build wheels serially from sdists, patching psutil on the way.
# Only psutil needs the sdist download + extract (for the android patch);
# every other package goes straight to `pip wheel` from its locked spec.
log "Building wheels from sdist (serial)"
python3 - "$WORK" "$BUILD_SET" "$WHEELHOUSE" <<'PYEOF' || fail "wheel building failed"
import subprocess, sys, tarfile, tempfile, os, shutil
from pathlib import Path, PurePosixPath

work, build_set, wheelhouse = (Path(a) for a in sys.argv[1:4])
entries = [
    (parts[0], " ".join(parts[1:]))
    for parts in (l.split(None, 1) for l in build_set.read_text(encoding="utf-8").splitlines() if l.strip())
]

def safe_extract(archive: Path, dest: Path) -> Path:
    """Safe-extract a tarball, rejecting traversal/symlink/device members.

    Shaped after the deleted hermes_cli/psutil_android.py (git show
    f8236a2d91~1:hermes_cli/psutil_android.py).
    """
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
    roots = sorted(p for p in dest.iterdir() if p.is_dir() and p.name.startswith("psutil"))
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

for name, spec in entries:
    req = f"{name} {spec}".strip() if spec else name
    if name == "psutil":
        print(f"==> building {name} (download + extract + android patch)")
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
            subprocess.run(
                [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                 "-w", str(wheelhouse), str(src)],
                check=True, cwd=tmp,
            )
    else:
        print(f"==> building {name} (direct pip wheel)")
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
             "--no-binary", ":all:", "-w", str(wheelhouse), req],
            check=True,
        )
PYEOF

# [i] Retag every built wheel to the PEP 738 android tag -- ONE in-process
# batch invocation (no per-wheel interpreter startup), absolute script path
# (correct regardless of cwd), stopping at the first error.
log "Retagging wheels to $PLATFORM_TAG"
RETAG_SCRIPT="$(cd "$(dirname "$0")" && pwd)/retag_wheel.py"
find "$WHEELHOUSE" -maxdepth 1 -name '*.whl' -print0 \
    | xargs -0 --no-run-if-empty python3 "$RETAG_SCRIPT" --platform-tag "$PLATFORM_TAG" \
    || fail "wheel retagging failed"
RETAGGED="$(find "$WHEELHOUSE" -maxdepth 1 -name "*${PLATFORM_TAG}*.whl" | wc -l | tr -d ' ')"
[ "$RETAGGED" -gt 0 ] || fail "no wheels retagged to $PLATFORM_TAG"
log "Retagged $RETAGGED wheels"

# [j] Completeness gate: binary-only offline install of the FULL resolved
# graph (not just the built set) into a clean venv, then import + check.
log "Completeness gate: --only-binary :all: --no-index install"
VENV="$WORK/verify-venv"
uv venv "$VENV" --python "$(command -v python3)" >/dev/null \
    || fail "uv venv failed for the verification venv"
uv pip install --python "$VENV/bin/python" \
    --only-binary=:all: --no-index --find-links "$WHEELHOUSE" \
    -r "$WORK/req.txt" \
    || fail "completeness gate FAILED: the wheelhouse does not cover the resolved graph offline"
uv pip check --python "$VENV/bin/python" \
    || fail "uv pip check failed after offline install"

log "Importing every native module"
# Single interpreter process inside the venv: one warm-up cost, imports all
# names, reports EVERY failure (not just the first). Dist name -> import
# name via importlib.metadata.packages_distributions() reversed (pyyaml ->
# yaml, etc.), falling back to the normalized dist name.
"$VENV/bin/python" - "$WORK/req.txt" <<'PYEOF' || fail "native import gate failed"
import importlib, importlib.metadata as md, re, sys

req = sys.argv[1]
dist_names = []
for line in open(req, encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("--"):
        continue
    m = re.match(r"^([A-Za-z0-9._-]+)", line)
    if m:
        dist_names.append(m.group(1))

import_to_dists = md.packages_distributions()
dists_to_imports: dict[str, str] = {}
for import_name, dists in import_to_dists.items():
    for dist in dists:
        dists_to_imports.setdefault(dist.replace("-", "_").lower(), import_name)

failures = []
for dist in dist_names:
    import_name = dists_to_imports.get(dist.replace("-", "_").lower(), dist.replace("-", "_"))
    try:
        importlib.import_module(import_name)
        print(f"  imported {import_name}")
    except Exception as exc:  # noqa: BLE001 -- report every failure, then exit
        failures.append((dist, import_name, f"{type(exc).__name__}: {exc}"))

if failures:
    print(f"{len(failures)} import failure(s):", file=sys.stderr)
    for dist, import_name, err in failures:
        print(f"  {dist} (import {import_name}): {err}", file=sys.stderr)
    raise SystemExit(1)
PYEOF

# [k] Emit the manifest artifacts -- platformTag from pins.json, pythonAbi
# derived from python.version; one digest computed per wheel (index.json +
# SHA256SUMS share it).
log "Emitting index.json, system-packages.txt, SHA256SUMS"
python3 - "$WHEELHOUSE" "$OUT_ABS" "$TAG" "$PINS" <<'PYEOF' || fail "manifest emission failed"
import hashlib, json, shutil, subprocess, sys
from pathlib import Path

wheelhouse, out, tag, pins_path = sys.argv[1:5]
pins = json.loads(Path(pins_path).read_text(encoding="utf-8"))
platform_tag = pins["wheel"]["platformTag"]
pv = pins["python"]["version"].split(".")
python_abi = f"cp{pv[0]}{pv[1]}"

wheels = sorted(Path(wheelhouse).glob("*.whl"))
digests = {w.name: hashlib.sha256(w.read_bytes()).hexdigest() for w in wheels}
index = {
    "schemaVersion": 1,
    "tag": tag,
    "platformTag": platform_tag,
    "pythonAbi": python_abi,
    "wheels": [{"name": w.name, "sha256": digests[w.name]} for w in wheels],
}
Path(out, "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
if shutil.which("dpkg-query") is None:
    print("system-packages: dpkg-query not found on this host", file=sys.stderr)
    syspkgs_text = "unavailable\n"
else:
    syspkgs = subprocess.run(["dpkg-query", "-W", "-f", "${Package} ${Version}\\n"],
                             capture_output=True, text=True, check=False)
    if syspkgs.returncode != 0:
        print(f"system-packages: dpkg-query failed (broken dpkg?), rc={syspkgs.returncode}: "
              f"{syspkgs.stderr.strip()}", file=sys.stderr)
    syspkgs_text = syspkgs.stdout or "unavailable\n"
Path(out, "system-packages.txt").write_text(syspkgs_text, encoding="utf-8")
with open(Path(out, "SHA256SUMS"), "w", encoding="utf-8", newline="\n") as f:
    for w in wheels:
        f.write(f"{digests[w.name]}  {w.name}\n")
print(f"  {len(wheels)} wheels indexed")
PYEOF

log "Wheelhouse complete: $WHEELHOUSE"
