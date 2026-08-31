#!/usr/bin/env bash
# Fat .deb assembly for the Termux hermes-agent bundle (Task 4 of
# .hermes/plans/2026-08-31_termux-deb.md). Runs AFTER termux_build.sh
# (wheelhouse) and build_cpython.sh / build_node.sh have populated the
# payload dir. No opt-out flags: a skipped step is a different artifact.
#
# Inputs (all required):
#   --repo <dir>          hermes-agent checkout (tag must exist; provenance)
#   --tag <tag>           immutable release tag (vX.Y.Z or vX.Y.Z-nightly.<ts>)
#   --payload <dir>       dir containing python/, node/, app/ (git archive of
#                         the tag) and wheelhouse/ (from termux_build.sh)
#   --out <dir>           output dir; <out>/hermes-agent_<v>_arm64.deb lands here
#
# No opt-out flags: the .deb is ALWAYS installed into a fresh run of the
# pinned termux-docker image (digest pinned in pm/lock.json) and smoke-tested.
# docker must be available. The channel is derived from the tag by
# deb_version.py (--channel), not passed in.
#
# Staged payload layout: python/ and node/ are pm-staged termux .deb
# trees ($PREFIX-shaped: data/data/com.termux/files/usr/...). The
# installed layout is $PREFIX/lib/hermes-agent/{python,node,app,venv,bin} with
# exactly one leak: $PREFIX/bin/hermes -> lib/hermes-agent/bin/hermes.

set -Eeuo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# The container digest is a pm pin (the termux-docker package); read it
# from the single lock beside every other third-party artifact pin.
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

REPO=""
TAG=""
PAYLOAD=""
OUT=""

usage() { printf 'usage: build_deb.sh --repo <dir> --tag <tag> --payload <dir> --out <dir>\n' >&2; exit 2; }
log()  { printf '\n==> %s\n' "$*"; }
fail() { printf 'build_deb: FAILED: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo) REPO="${2:?}"; shift 2 ;;
        --tag) TAG="${2:?}"; shift 2 ;;
        --payload) PAYLOAD="${2:?}"; shift 2 ;;
        --out) OUT="${2:?}"; shift 2 ;;
        *) usage ;;
    esac
done
[ -n "$REPO" ] && [ -n "$TAG" ] && [ -n "$PAYLOAD" ] && [ -n "$OUT" ] || usage

for tool in python3 docker dpkg-deb jq; do
    command -v "$tool" >/dev/null || fail "missing tool: $tool"
done

REPO_ABS="$(cd "$REPO" && pwd)"
PAYLOAD_ABS="$(cd "$PAYLOAD" && pwd)"
OUT_ABS="$(mkdir -p "$OUT" && cd "$OUT" && pwd)"

# [0] Provenance: the tag must be real in the checkout; the payload must be
# the built tree of that checkout, not some other directory. The commit is
# captured ONCE here and reused for the install stamp below.
COMMIT="$(git -C "$REPO_ABS" rev-parse --verify "refs/tags/$TAG^{commit}")" \
    || fail "tag $TAG not found in $REPO_ABS"
for d in python node app wheelhouse; do
    [ -d "$PAYLOAD_ABS/$d" ] || fail "payload missing $d/ -- run termux_build.sh + build_cpython.sh + build_node.sh first"
done
PYBIN_REL="data/data/com.termux/files/usr/bin/python3.11"
[ -f "$PAYLOAD_ABS/python/$PYBIN_REL" ] || fail "payload python tree lacks $PYBIN_REL"
NODEBIN_REL="data/data/com.termux/files/usr/bin/node"
[ -f "$PAYLOAD_ABS/node/$NODEBIN_REL" ] || fail "payload node tree lacks $NODEBIN_REL"

PKG="hermes-agent"

# [1] Version derivation: pure function in deb_version.py, tested separately.
log "Deriving Debian version from tag $TAG"
DEB_VERSION="$(python3 "$HERE/deb_version.py" "$TAG")" || fail "version derivation failed for tag $TAG"
log "Package version: $DEB_VERSION"

# [2] Assemble the venv offline, INSIDE the pinned container: the staged
# interpreter is bionic/arm64 and cannot run on this host. Completeness is
# enforced by construction: --no-index means a missing wheel fails loudly.
# The container sees the payload at /payload; the venv is built beside the
# staged trees so the shipped venv's absolute shebangs point at the REAL
# $PREFIX path they will occupy on-device ($PREFIX is contractual).
log "Creating venv with the bundled CPython (inside the container)"
if [ -d "$PAYLOAD_ABS/venv" ]; then rm -rf "$PAYLOAD_ABS/venv"; fi
# The bind mount is runner-owned: the container (any uid) can only write
# into a dir the HOST pre-created with open perms (same as the wheelhouse).
mkdir -p "$PAYLOAD_ABS/venv"
chmod 0777 "$PAYLOAD_ABS/venv"
docker run --rm --platform linux/arm64 \
    --user root \
    -v "$PAYLOAD_ABS:/payload" \
    "$IMAGE" bash -c '
        set -euo pipefail
        export PREFIX=/data/data/com.termux/files/usr
        export PATH="$PREFIX/bin:${PATH:-/usr/bin:/bin}"
        # The staged binary is dynamically linked against its OWN tree lib;
        # the container linker needs to be told where it lives (same fix as
        # the wheelhouse container half).
        export LD_LIBRARY_PATH="/payload/python$PREFIX/lib:/payload/node$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        # The staged tree is mounted at its REAL $PREFIX path so the venv
        # recorded absolute paths are correct on-device from birth.
        mkdir -p "$PREFIX" 2>/dev/null || true
        PY="/payload/python$PREFIX/bin/python3.11"
        UV="/payload/uv$PREFIX/bin/uv"
        # The staged python bundled ensurepip fails in this environment;
        # the STAGED uv creates the venv and installs (the exact pattern
        # the wheelhouse container proved end-to-end).
        "$UV" venv --python "$PY" --seed /payload/venv
        "$UV" pip install --python /payload/venv/bin/python \n            --no-index --find-links /payload/wheelhouse /payload/app
        "$UV" pip check --python /payload/venv/bin/python
    ' || fail "venv assembly failed inside the container (offline wheelhouse install)"

# [3] Trampolines: POSIX sh, resolve their own dir, dispatch on the bundled
# python. Installed under $PREFIX/lib/hermes-agent/bin; ../python is a sibling.
log "Writing trampolines"
mkdir -p "$PAYLOAD_ABS/bin"
cat > "$PAYLOAD_ABS/bin/hermes" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
# hermes trampoline -- resolve own dir, exec the bundled venv's entrypoint.
self_dir="$(cd "$(dirname "$0")" && pwd)"
exec "$self_dir/../venv/bin/python" -m hermes_cli.main "$@"
EOF
cat > "$PAYLOAD_ABS/bin/hermes-agent" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
# hermes-agent trampoline -- runs the agent entrypoint.
self_dir="$(cd "$(dirname "$0")" && pwd)"
exec "$self_dir/../venv/bin/python" -m hermes_cli.run_agent "$@"
EOF
cat > "$PAYLOAD_ABS/bin/hermes-acp" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
# hermes-acp trampoline -- ACP adapter entrypoint.
self_dir="$(cd "$(dirname "$0")" && pwd)"
exec "$self_dir/../venv/bin/python" -m hermes_cli.acp "$@"
EOF
chmod 755 "$PAYLOAD_ABS/bin/hermes" "$PAYLOAD_ABS/bin/hermes-agent" "$PAYLOAD_ABS/bin/hermes-acp"

# [4] Install stamp: provenance for the steward contract (distribution
# apt-termux -> update/uninstall refuse with pkg remediation). Written by the
# canonical writer (same one docker/nix/desktop use) so the schema stays
# identical across packagers; the tag rides in via HERMES_PAYLOAD_TAG.
log "Writing app/install-stamp.json"
HERMES_PAYLOAD_TAG="$TAG" \
HERMES_DESKTOP_VARIANT=bundled \
python3 "$REPO_ABS/scripts/write_install_stamp.py" \
    --output "$PAYLOAD_ABS/app/install-stamp.json" \
    --commit "$COMMIT" \
    --distribution apt-termux \
    --update-mechanism external \
    --source bundle \
    || fail "stamp write failed"

# [5]+[6] Staging dir: DEBIAN/ control + payload under lib/hermes-agent/.
log "Staging the package tree"
STAGE="$OUT_ABS/.stage-$DEB_VERSION"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/lib/hermes-agent"
cp -a "$PAYLOAD_ABS/python" "$PAYLOAD_ABS/node" "$PAYLOAD_ABS/app" "$PAYLOAD_ABS/venv" "$PAYLOAD_ABS/bin" "$STAGE/lib/hermes-agent/"

# postinst/prerm: manage the ONE leak, $PREFIX/bin/hermes, idempotently.
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
# Ensure $PREFIX/bin/hermes -> lib/hermes-agent/bin/hermes (idempotent).
LINK="$PREFIX/bin/hermes"
TARGET="../lib/hermes-agent/bin/hermes"
mkdir -p "$PREFIX/bin"
# Atomic: ln either creates the link or fails (EEXIST); never a
# check-then-create race. A pre-existing link is fine; any other ln failure
# is a loud nonzero exit, not a swallowed one.
ln -s "$TARGET" "$LINK" 2>/dev/null || [ -L "$LINK" ]
exit 0
EOF
cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
# Remove $PREFIX/bin/hermes if it points at us (never clobber a foreign file).
LINK="$PREFIX/bin/hermes"
if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "../lib/hermes-agent/bin/hermes" ]; then
    rm -f "$LINK"
fi
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $DEB_VERSION
Architecture: arm64
Maintainer: Nous Research
Description: Hermes Agent CLI for Termux (self-contained bundled python/node/venv)
Installed-Size: $(du -sk "$STAGE/lib" | cut -f1)
EOF
# Self-contained: no Depends line at all. Our python, node and venv ship inside.

# [7] Validation hook: install into a FRESH container of the pinned image and
# smoke-test the exact binaries the phone will run. No opt-out.
log "Validating in a fresh pinned termux-docker container"
DEB="$OUT_ABS/${PKG}_${DEB_VERSION}_arm64.deb"
rm -f "$DEB"
dpkg-deb --build --root-owner-group "$STAGE" "$DEB" || fail "dpkg-deb --build failed"
rm -rf "$STAGE"
[ -f "$DEB" ] || fail "dpkg-deb did not produce $DEB"

VD="$OUT_ABS/.deb-validate"
rm -rf "$VD"; mkdir -p "$VD"
cat > "$VD/check.sh" <<'CHECK'
set -eu
export PATH="$PREFIX/bin:$PATH"
dpkg -i /tmp/pkg.deb
echo "--- dpkg -L sanity ---"
dpkg -L hermes-agent | grep -q "$PREFIX/lib/hermes-agent/bin/hermes" \
    || { echo "FAIL: dpkg -L missing payload bin/hermes"; exit 1; }
dpkg -L hermes-agent | grep -q "$PREFIX/lib/hermes-agent/venv" \
    || { echo "FAIL: dpkg -L missing venv"; exit 1; }
echo "--- hermes --version ---"
"$PREFIX/bin/hermes" --version
echo "--- hermes update (must refuse with pkg remediation) ---"
set +e
UPD_OUT="$("$PREFIX/bin/hermes" update 2>&1)"
RC=$?
set -e
echo "$UPD_OUT"
[ "$RC" -ne 0 ] || { echo "FAIL: hermes update exited 0 -- it must refuse"; exit 1; }
echo "$UPD_OUT" | grep -q "pkg upgrade hermes-agent" \
    || { echo "FAIL: refusal does not mention 'pkg upgrade hermes-agent'"; exit 1; }
echo "VALIDATION OK"
CHECK
docker run --rm --platform linux/arm64 \
    -v "$DEB:/tmp/pkg.deb:ro" \
    -v "$VD/check.sh:/tmp/check.sh:ro" \
    "$IMAGE" sh /tmp/check.sh \
    || fail "container validation failed"
rm -rf "$VD"

log "Built $DEB (validated)"
