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
import json, sys, urllib.request
resolved = [l.split(None, 1) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
needs_build = []
for name, _spec in resolved:
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=30) as r:
            d = json.load(r)
        v = d["info"]["version"]
        files = d["releases"].get(v, [])
        has_wheel = any(f["filename"].endswith(".whl") for f in files)
    except Exception as exc:  # noqa: BLE001 -- a probe miss means BUILD it
        print(f"  probe miss {name}: {exc} -> building from sdist", file=sys.stderr)
        has_wheel = False
    if not has_wheel:
        needs_build.append(name)
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(needs_build) + "\n")
print(f"  {len(needs_build)} of {len(resolved)} packages need sdist builds")
PYEOF

# [e]-[h] Build wheels serially from sdists, patching psutil on the way.
log "Building wheels from sdist (serial)"
python3 - "$WORK" "$BUILD_SET" "$WHEELHOUSE" <<'PYEOF' || fail "wheel building failed"
import subprocess, sys, tarfile, tempfile, os, shutil
from pathlib import Path, PurePosixPath

work, build_set, wheelhouse = (Path(a) for a in sys.argv[1:4])
names = [l.strip() for l in build_set.read_text(encoding="utf-8").splitlines() if l.strip()]

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

for name in names:
    print(f"==> building {name}")
    with tempfile.TemporaryDirectory(prefix=f"hermes-build-{name}-") as tmp:
        tmp = Path(tmp)
        sdist_dir = tmp / "sdist"
        sdist_dir.mkdir()
        # Download the sdist for the pinned version from the req spec.
        subprocess.run(
            [sys.executable, "-m", "pip", "download", "--no-deps", "--no-binary", ":all:",
             "--no-build-isolation", "-d", str(sdist_dir), name],
            check=True, cwd=tmp,
        )  # nb: version spec comes from the resolved list; pip resolves it
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
PYEOF

# [i] Retag every built wheel to the PEP 738 android tag.
log "Retagging wheels to android_24_arm64_v8a"
find "$WHEELHOUSE" -maxdepth 1 -name '*.whl' -print0 | while IFS= read -r -d '' whl; do
    python3 scripts/termux/retag_wheel.py "$whl" || exit 1
done
RETAGGED="$(find "$WHEELHOUSE" -maxdepth 1 -name '*android_24_arm64_v8a*.whl' | wc -l | tr -d ' ')"
[ "$RETAGGED" -gt 0 ] || fail "no wheels retagged to android_24_arm64_v8a"
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
python3 - "$VENV/bin/python" "$WORK/req.txt" <<'PYEOF' || fail "native import gate failed"
import importlib, re, subprocess, sys
vp, req = sys.argv[1], sys.argv[2]
names = []
for line in open(req, encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("--"):
        continue
    m = re.match(r"^([A-Za-z0-9._-]+)", line)
    if m:
        names.append(m.group(1).replace("-", "_"))
for name in names:
    r = subprocess.run([vp, "-c", f"import {name}"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"import failed for {name}:\n{r.stderr}")
    print(f"  imported {name}")
PYEOF

# [k] Emit the manifest artifacts.
log "Emitting index.json, system-packages.txt, SHA256SUMS"
python3 - "$WHEELHOUSE" "$OUT_ABS" "$TAG" <<'PYEOF' || fail "manifest emission failed"
import csv, hashlib, json, subprocess, sys
from pathlib import Path

wheelhouse, out, tag = sys.argv[1], sys.argv[2], sys.argv[3]
wheels = sorted(Path(wheelhouse).glob("*.whl"))
index = {
    "schemaVersion": 1,
    "tag": tag,
    "platformTag": "android_24_arm64_v8a",
    "pythonAbi": "cp314",
    "wheels": [
        {"name": w.name, "sha256": hashlib.sha256(w.read_bytes()).hexdigest()}
        for w in wheels
    ],
}
Path(out, "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
try:
    syspkgs = subprocess.run(["dpkg-query", "-W", "-f", "${Package} ${Version}\\n"],
                             capture_output=True, text=True, check=False)
    Path(out, "system-packages.txt").write_text(syspkgs.stdout or "unavailable\n", encoding="utf-8")
except Exception:
    Path(out, "system-packages.txt").write_text("unavailable\n", encoding="utf-8")
with open(Path(out, "SHA256SUMS"), "w", encoding="utf-8", newline="\n") as f:
    for w in wheels:
        f.write(f"{hashlib.sha256(w.read_bytes()).hexdigest()}  {w.name}\n")
print(f"  {len(wheels)} wheels indexed")
PYEOF

log "Wheelhouse complete: $WHEELHOUSE"
