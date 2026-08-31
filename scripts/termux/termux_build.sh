#!/usr/bin/env bash
# Android/Termux wheelhouse builder -- the ONE entry point. Two halves:
#
#   HOST half (glibc runner): tag gates, archive, uv resolve, marker-aware
#   PyPI probe -> resolved.txt + build_set.txt. Pure data work; arch-free.
#
#   CONTAINER half (bionic termux-docker): toolchain pins, the 13-ish native
#   sdist builds (clang/rust against the container's own termux python),
#   PEP 738 retag, the --no-index completeness gate, and the import gate.
#   The wheels MUST be bionic: building them on the glibc host would ship
#   glibc binaries that cannot exec on any phone.
#
# The script re-invokes ITSELF with --in-container inside the digest-pinned
# image (from pm/lock.json's termux-docker package). No opt-out flags.
#
# Inputs (host mode, all required):
#   --repo <dir>   hermes-agent checkout to build from (must contain the tag)
#   --tag <tag>    immutable release tag (vX.Y.Z or vX.Y.Z-nightly.<ts>)
#   --out <dir>    output dir (wheelhouse/ + index.json + SHA256SUMS land here)

set -Eeuo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/termux/build_config.sh
. "$HERE/build_config.sh"

log() { printf '\n==> %s\n' "$*"; }
fail() { printf 'termux_build: FAILED: %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--in-container" ]; then
    # =================== CONTAINER HALF (bionic) =======================
    RESOLVED="$2"; BUILD_SET="$3"; WHEELHOUSE="$4"
    # $5 (optional) is the staged payload root (mounted at /payload):
    # the wheels are built with THE PAYLOAD'S OWN python -- the TUR .deb
    # pm staged from the lock -- so the ABI is the shipped ABI by
    # construction and the offline gate installs into a venv of the very
    # interpreter the phone will run.
    PAYLOAD_ROOT="${5:-}"
    export PREFIX=/data/data/com.termux/files/usr
    STAGED_PY="$PAYLOAD_ROOT/python$PREFIX/bin/python3.11"
    STAGED_UV="$PAYLOAD_ROOT/uv$PREFIX/bin/uv"
    # The staged binaries' RUNPATHs point at the phone's $PREFIX layout
    # (linkerconfig on-device). Inside the container the tree lives at
    # $PAYLOAD_ROOT/python$PREFIX, so the dynamic linker needs to be told
    # where the payload's libs live before any staged binary runs.
    export LD_LIBRARY_PATH="$PAYLOAD_ROOT/python$PREFIX/lib:$PAYLOAD_ROOT/node$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    if [ -n "$PAYLOAD_ROOT" ] && [ -x "$STAGED_PY" ] && [ -x "$STAGED_UV" ]; then
        PY="$STAGED_PY"
        UV="$STAGED_UV"
        log "Using the staged payload python ($PY) and uv ($UV)"
    else
        # No payload staged (or missing): refuse. Building against any
        # other interpreter (the container's pkg python is 3.14!) ships
        # wheels the payload venv cannot install. No silent fallback.
        fail "staged payload python missing at $STAGED_PY -- stage the payload (pm lock rows) before the wheelhouse"
    fi
    # BUILD tools come from termux's own apt (the image is a bare
    # bootstrap). The USER machine never does any of this.
    if ! command -v clang >/dev/null 2>&1; then
        log "Provisioning the container build toolchain (termux apt)"
        export DEBIAN_FRONTEND=noninteractive
        # Pin the OFFICIAL mirror and use apt DIRECTLY: pkg (the wrapper)
        # re-runs mirror selection and rewrote our pin to a desynced
        # third-party mirror mid-run (live 404 on libexpat). apt respects
        # sources.list as written. Retry the update for propagation windows.
        printf '%s\n' "deb https://packages.termux.dev/apt/termux-main stable main" \
            > "$PREFIX/etc/apt/sources.list"
        rm -f "$PREFIX/etc/apt/sources.list.d"/*.list 2>/dev/null || true
        apt update || apt update \
            || fail "apt update failed in the container"
        apt install -y clang rust make patchelf binutils pkg-config protobuf cmake ninja \
            libandroid-posix-semaphore libandroid-support libbz2 libffi \
            libjpeg-turbo libpng freetype libtiff libwebp openjpeg littlecms \
            libyaml openssl readline zlib liblzma libsqlite ncurses \
            || fail "apt install of the build toolchain failed"
    fi
    # BINARIES, not package names: the rust package provides rustc/cargo
    # (there is no `rust` binary).
    for tool in clang rustc cargo make; do
        command -v "$tool" >/dev/null 2>&1 \
            || fail "container lacks $tool after provisioning"
    done
    # Serial builds only -- parallel Rust/C builds OOM arm runners.
    export CARGO_BUILD_JOBS=1
    export MAKEFLAGS=-j1
    export UV_CONCURRENT_BUILDS=1
    # Native extension links need the STAGED payload's libpython: the
    # container's own $PREFIX/lib (bootstrap only) is on the default
    # -L path, but libpython3.11.so lives in the staged tree. setuptools
    # honors LDFLAGS, so every sdist build's link step finds it.
    STAGED_PYLIB="$PAYLOAD_ROOT/python$PREFIX/lib"
    export LDFLAGS="-L$STAGED_PYLIB ${LDFLAGS:-}"
    export CFLAGS="-I$PAYLOAD_ROOT/python$PREFIX/include ${CFLAGS:-}"
    # maturin (rust-backend sdists: cryptography, pydantic-core, ...) needs
    # the Android API level explicitly on a non-phone host. Matches the
    # wheel platform tag (android_24_arm64_v8a).
    export ANDROID_API_LEVEL=24
    # cargo composes its OWN link line (ignores LDFLAGS); pyo3 finds the
    # python BINARY via PYO3_PYTHON but the -lpython lib search path must
    # come through RUSTFLAGS, which cargo forwards to the linker.
    export RUSTFLAGS="-L$STAGED_PYLIB ${RUSTFLAGS:-}"
    # protoc-bin-vendored ships no android binary; the termux protobuf
    # package provides one, and PROTOC_BIN_PATH points vendored crates
    # at it (nemo-relay's worker-proto otherwise fails codegen).
    export PROTOC="$PREFIX/bin/protoc"
    export PROTOC_BIN_PATH="$PREFIX/bin/protoc"
    log "Creating the scratch build venv + enforcing toolchain pins (staged uv)"
    # The container runs as its own uid: /out (runner-owned) and /tmp are
    # NOT writable, but the container's own termux prefix IS (provisioning
    # already writes there). Scratch venv under $PREFIX/tmp.
    mkdir -p "$PREFIX/tmp"
    BUILD_VENV="$PREFIX/tmp/hermes-build-venv"
    "$UV" venv --python "$PY" "$BUILD_VENV" \
        || fail "scratch build venv creation failed"
    # uv venvs ship WITHOUT pip; the sdist build loop shells out to
    # `python -m pip wheel`, which needs pip INSIDE the venv. uv installs
    # it from outside (the only tool that can, cleanly, on bionic).
    "$UV" pip install --python "$BUILD_VENV/bin/python" pip \
        || fail "pip bootstrap into the build venv failed"
    "$UV" pip install --python "$BUILD_VENV/bin/python" "${TOOLCHAIN_PINS[@]}" \
        || fail "toolchain pin enforcement failed"
    log "Building the android wheel set from sdist (bionic, payload ABI)"
    "$BUILD_VENV/bin/python" "$HERE/build_wheels.py" \
        --resolved "$RESOLVED" --build-set "$BUILD_SET" \
        --wheelhouse "$WHEELHOUSE" --retag "$HERE/retag_wheel.py" \
        --platform-tag "$PLATFORM_TAG" --uv "$UV" \
        || fail "wheel building failed"
    log "Wheelhouse container phase complete"
    exit 0
fi

# ===================== HOST HALF (glibc runner) ==========================
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

for tool in uv git curl docker python3; do
    command -v "$tool" >/dev/null 2>&1 \
        || fail "missing tool: $tool (CI must provision the pinned toolchain before running this script)"
done

ARCH="$(uname -m)"
case "$ARCH" in
    aarch64|arm64) ;;
    *) fail "refusing to build on non-aarch64 host (uname -m: $ARCH)" ;;
esac

# [b] FIRST: refuse mutable releases.
log "Verifying release tag $TAG exists on origin"
git -C "$REPO" ls-remote --exit-code --tags origin "$TAG" >/dev/null \
    || fail "tag $TAG not found on origin; refusing to build a mutable release"
if command -v gh >/dev/null 2>&1; then
    gh release view "$TAG" --repo "$(git -C "$REPO" remote get-url origin | sed -e 's#.*github.com[:/]##' -e 's#\.git$##')" >/dev/null 2>&1 \
        || fail "release $TAG not found; refusing to build before the release exists"
fi

REPO_ABS="$(cd "$REPO" && pwd)"
OUT_ABS="$(mkdir -p "$OUT" && cd "$OUT" && pwd)"
WORK="$OUT_ABS/.work"
WHEELHOUSE="$OUT_ABS/wheelhouse"
rm -rf "$WORK" "$WHEELHOUSE"
mkdir -p "$WORK" "$WHEELHOUSE"

# [c] Stage the tag as a gitless tree.
log "Archiving $TAG into $WORK/tree"
git -C "$REPO_ABS" archive --format=tar "$TAG" | tar -xf - -C "$WORK/tree" 2>/dev/null || {
    mkdir -p "$WORK/tree"
    git -C "$REPO_ABS" archive --format=tar -o "$WORK/tree.tar" "$TAG"
    tar -xf "$WORK/tree.tar" -C "$WORK/tree"
    rm -f "$WORK/tree.tar"
}
[ -f "$WORK/tree/pyproject.toml" ] || fail "archived tag tree has no pyproject.toml -- bad tag?"

# [d] Resolve the real graph from the tag's own lock.
log "Resolving dependency graph from the tag's uv.lock"
( cd "$WORK/tree" && uv export --frozen --no-emit-project -o "$WORK/req.txt" ) \
    || fail "uv export failed (frozen lock at $TAG)"
RESOLVED="$WORK/resolved.txt"
python3 - "$WORK/req.txt" "$RESOLVED" <<'PYEOF' || fail "failed to normalize requirements"
import re, sys
out = []
for line in open(sys.argv[1], encoding="utf-8"):
    line = line.strip()
    # uv export wraps long lines with backslash continuations; strip the
    # trailing continuation BEFORE capturing markers/specs -- a backslash
    # riding into a marker makes Marker() throw (and a throwing marker
    # must not silently admit the package into the build set).
    line = re.sub(r"\\\s*$", "", line)
    if not line or line.startswith("#"):
        continue
    marker = ""
    m = re.search(r"\s;\s*(.+)$", line)
    if m:
        marker = m.group(1).strip()
        line = line[: m.start()]
    line = re.sub(r"\s*--.*$", "", line)
    if line.startswith("--") or not line:
        continue
    m = re.match(r"^([A-Za-z0-9._-]+)(\[[^]]*\])?([=<>~!^].*)?$", line)
    if m:
        out.append((m.group(1), m.group(3) or "", marker))
open(sys.argv[2], "w", encoding="utf-8").write(
    "\n".join(f"{name}\t{spec}\t{marker}" for name, spec, marker in out) + "\n"
)
PYEOF
[ -s "$RESOLVED" ] || fail "resolved dependency list is empty"

# [e] Marker-aware PyPI wheel-coverage probe: build set = resolved deps
# whose marker admits android AND that have no installable none-any wheel.
log "Probing PyPI wheel coverage (android markers)"
BUILD_SET="$WORK/build_set.txt"
python3 - "$RESOLVED" "$BUILD_SET" <<'PYEOF' || fail "PyPI wheel-coverage probe failed"
import json, re, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor

# The TARGET environment the wheelhouse must satisfy: Termux's bionic
# python. TUR 3.11 reports sys.platform "linux" (the android value only
# arrived in 3.13), so markers keying on linux admit it -- and windows/
# darwin markers exclude it, which is the whole point.
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

def locked_version(spec: str) -> str | None:
    m = re.search(r"==\s*([A-Za-z0-9._+!-]+)", spec or "")
    return m.group(1) if m else None

def marker_admits(marker: str) -> bool:
    if not marker:
        return True
    from packaging.markers import Marker
    try:
        return Marker(marker).evaluate(TARGET_ENV)
    except Exception:
        # An unevaluable marker is a build-set decision, not a silent
        # exclude: admit it so the wheel build surfaces the truth loudly.
        return True

entries = []
for line in open(sys.argv[1], encoding="utf-8"):
    if not line.strip():
        continue
    name, spec, marker = (line.split("\t", 2) + ["", ""])[:3]
    entries.append((name, spec, marker))

def probe(item):
    name, spec, marker = item
    if not marker_admits(marker):
        return name, None, None
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=30) as r:
            d = json.load(r)
        locked = locked_version(spec) or d["info"]["version"]
        files = d["releases"].get(locked, [])
        # Coverage means INSTALLABLE on android/bionic, not "a wheel exists":
        # only py3-none-any wheels satisfy a package; anything else installs
        # nowhere on termux and must be built from sdist here.
        covered = any(
            f["filename"].endswith(".whl") and "-none-any.whl" in f["filename"]
            for f in files
        )
        return name, covered, None
    except Exception as exc:  # noqa: BLE001 -- a probe miss means BUILD it
        return name, False, str(exc)

with ThreadPoolExecutor(max_workers=10) as ex:
    results = list(ex.map(probe, entries))

# Documented android build misses: upstream packages whose vendored
# toolchain excludes android and cannot be built without a fork.
# nemo-relay: worker-proto uses protoc-bin-vendored, which ships no
# android protoc and fails codegen regardless of PROTOC* env (verified
# live twice). The .deb ships without the relay exporter.
ANDROID_BUILD_MISSES = {
    "nemo-relay": "protoc-bin-vendored ships no android protoc (upstream)",
}

needs_build = []
excluded = 0
missed = []
for name, covered, err in results:
    if covered is None:
        excluded += 1
        continue
    if err is not None:
        print(f"  probe miss {name}: {err} -> building from sdist", file=sys.stderr)
    if not covered:
        if name in ANDROID_BUILD_MISSES:
            missed.append((name, ANDROID_BUILD_MISSES[name]))
            continue
        needs_build.append(name)
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(needs_build) + "\n")
print(f"  {excluded} of {len(entries)} deps are marker-excluded for android")
for name, why in missed:
    print(f"  documented build miss: {name} ({why})")
print(f"  {len(needs_build)} of {len(entries) - excluded} applicable packages need sdist builds")
PYEOF
[ -s "$BUILD_SET" ] || fail "build set is empty -- nothing to build (probe bug?)"

# [f] Container digest comes from pm/lock.json (termux-docker package).
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
DIGEST="$(python3 - "$REPO_ROOT" <<'PYD'
import sys
sys.path.insert(0, sys.argv[1])
from pm.lock import Lockfile
from pm.paths import lockfile_path
print(Lockfile(lockfile_path()).version("termux-docker"))
PYD
)" || fail "failed to read the termux-docker digest from pm/lock.json"
[ -n "$DIGEST" ] || fail "termux-docker digest missing from pm/lock.json"
IMAGE="termux/termux-docker@$DIGEST"

# [g] The build itself runs in the pinned container: the wheels must be
# bionic, and they are built with THE PAYLOAD'S OWN staged python (the
# exact TUR .deb pm staged from the lock), so the ABI matches the shipped
# interpreter by construction. The payload must be staged before this.
log "Building the wheelhouse inside the pinned container (payload ABI)"
PAYLOAD_ABS="$OUT_ABS"
[ -f "$PAYLOAD_ABS/python/data/data/com.termux/files/usr/bin/python3.11" ] \
    || fail "staged payload python missing -- run build_cpython.sh first (the wheelhouse builds with the payload interpreter)"
# The container mounts OUT_ABS at /out; translate the host-side work
# paths before crossing the boundary (host absolutes do not exist inside).
# The wheelhouse must be container-WRITABLE: /out is runner-owned, so
# pre-create the dir with open perms (the build writes wheels there).
mkdir -p "$OUT_ABS/wheelhouse"
chmod 0777 "$OUT_ABS/wheelhouse"
C_RESOLVED="/out/.work/resolved.txt"
C_BUILD_SET="/out/.work/build_set.txt"
C_WHEELHOUSE="/out/wheelhouse"
docker run --rm --platform linux/arm64 \
    -v "$REPO_ROOT:/repo" \
    -v "$OUT_ABS:/out" \
    "$IMAGE" bash /repo/scripts/termux/termux_build.sh \
        --in-container "$C_RESOLVED" "$C_BUILD_SET" "$C_WHEELHOUSE" "/out" \
    || fail "container wheelhouse build failed"

# [h] Emit the manifest artifacts (host side: pure data over the results).
log "Emitting index.json, system-packages.txt, SHA256SUMS"
python3 - "$WHEELHOUSE" "$OUT_ABS" "$TAG" "$PLATFORM_TAG" "$PYTHON_ABI" <<'PYEOF' || fail "manifest emission failed"
import hashlib, json, subprocess, sys
from pathlib import Path

wheelhouse, out, tag, platform_tag, python_abi = sys.argv[1:6]
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
try:
    syspkgs = subprocess.run(["dpkg-query", "-W", "-f", "${Package} ${Version}\n"],
                             capture_output=True, text=True, check=False)
    Path(out, "system-packages.txt").write_text(syspkgs.stdout or "unavailable\n", encoding="utf-8")
except Exception:
    Path(out, "system-packages.txt").write_text("unavailable\n", encoding="utf-8")
with open(Path(out, "SHA256SUMS"), "w", encoding="utf-8", newline="\n") as f:
    for w in wheels:
        f.write(f"{digests[w.name]}  {w.name}\n")
print(f"  {len(wheels)} wheels indexed")
PYEOF

log "Wheelhouse complete: $WHEELHOUSE"
