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
#   --channel stable|nightly
#   --validate-container  REQUIRED. Present on every invocation including CI.
#                         The .deb is installed into a fresh run of the pinned
#                         termux-docker image (digest from pins.json) and
#                         smoke-tested. docker must be available.
#
# Installed layout: $PREFIX/lib/hermes-agent/{python,node,app,venv,bin} with
# exactly one leak: $PREFIX/bin/hermes -> lib/hermes-agent/bin/hermes.

set -Eeuo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PINS="$HERE/pins.json"

REPO=""
TAG=""
PAYLOAD=""
OUT=""
CHANNEL=""
VALIDATE_CONTAINER=""

usage() { printf 'usage: build_deb.sh --repo <dir> --tag <tag> --payload <dir> --out <dir> --channel stable|nightly --validate-container\n' >&2; exit 2; }
log()  { printf '\n==> %s\n' "$*"; }
fail() { printf 'build_deb: FAILED: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo) REPO="${2:?}"; shift 2 ;;
        --tag) TAG="${2:?}"; shift 2 ;;
        --payload) PAYLOAD="${2:?}"; shift 2 ;;
        --out) OUT="${2:?}"; shift 2 ;;
        --channel) CHANNEL="${2:?}"; shift 2 ;;
        --validate-container) VALIDATE_CONTAINER=1; shift ;;
        *) usage ;;
    esac
done
[ -n "$REPO" ] && [ -n "$TAG" ] && [ -n "$PAYLOAD" ] && [ -n "$OUT" ] && [ -n "$CHANNEL" ] || usage
case "$CHANNEL" in
    stable|nightly) ;;
    *) usage ;;
esac
# No opt-out: the validation hook is mandatory, not optional. The flag must be
# present (CI always passes it) and docker must exist.
[ "$VALIDATE_CONTAINER" = "1" ] || {
    fail "--validate-container is REQUIRED: the .deb must be proven in a fresh pinned container before it exists"
}

for tool in python3 docker dpkg-deb jq; do
    command -v "$tool" >/dev/null || fail "missing tool: $tool"
done

REPO_ABS="$(cd "$REPO" && pwd)"
PAYLOAD_ABS="$(cd "$PAYLOAD" && pwd)"
OUT_ABS="$(mkdir -p "$OUT" && cd "$OUT" && pwd)"

# [0] Provenance: the tag must be real in the checkout; the payload must be
# the built tree of that checkout, not some other directory.
git -C "$REPO_ABS" rev-parse --verify --quiet "refs/tags/$TAG^{commit}" >/dev/null \
    || fail "tag $TAG not found in $REPO_ABS"
for d in python node app wheelhouse; do
    [ -d "$PAYLOAD_ABS/$d" ] || fail "payload missing $d/ -- run termux_build.sh + build_cpython.sh + build_node.sh first"
done
[ -x "$PAYLOAD_ABS/python/bin/python3" ] || fail "payload python has no executable python3"

DIGEST="$(jq -r .termuxDocker.digest "$PINS")"
IMAGE="termux/termux-docker@$DIGEST"
PKG="hermes-agent"

# [1] Version derivation: pure function in deb_version.py, tested separately.
log "Deriving Debian version from tag $TAG"
DEB_VERSION="$(python3 "$HERE/deb_version.py" "$TAG")" || fail "version derivation failed for tag $TAG"
log "Package version: $DEB_VERSION"

# [2] Assemble the venv offline against OUR bundled python. Completeness is
# enforced by construction: --no-index means a missing wheel fails loudly.
log "Creating venv with the bundled CPython"
if [ -d "$PAYLOAD_ABS/venv" ]; then rm -rf "$PAYLOAD_ABS/venv"; fi
"$PAYLOAD_ABS/python/bin/python3" -m venv "$PAYLOAD_ABS/venv" \
    || fail "bundled python could not create a venv"
"$PAYLOAD_ABS/venv/bin/python" -m pip install --no-index --no-cache-dir --find-links "$PAYLOAD_ABS/wheelhouse" \
    "$PAYLOAD_ABS/app" \
    || fail "offline install of the app + full graph failed: the wheelhouse does not cover the lock"
"$PAYLOAD_ABS/venv/bin/python" -m pip check \
    || fail "pip check failed inside the assembled venv"

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
# apt-termux -> update/uninstall refuse with pkg remediation).
log "Writing app/install-stamp.json"
COMMIT="$(git -C "$REPO_ABS" rev-list -n1 "$TAG")"
python3 - "$PAYLOAD_ABS/app/install-stamp.json" "$COMMIT" "$TAG" <<'PYEOF' || fail "stamp write failed"
import json, sys
path, commit, tag = sys.argv[1], sys.argv[2], sys.argv[3]
stamp = {
    "schemaVersion": 2,
    "commit": commit,
    "distribution": "apt-termux",
    "source": "bundle",
    "updateMechanism": "external",
    "tag": tag,
}
open(path, "w", encoding="utf-8").write(json.dumps(stamp, indent=2) + "\n")
PYEOF

# [5]+[6] Staging dir: DEBIAN/ control + payload under lib/hermes-agent/.
log "Staging the package tree"
STAGE="$OUT_ABS/.stage-$DEB_VERSION"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/lib/hermes-agent"
cp -a "$PAYLOAD_ABS/python" "$PAYLOAD_ABS/node" "$PAYLOAD_ABS/app" "$PAYLOAD_ABS/venv" "$PAYLOAD_ABS/bin" "$STAGE/lib/hermes-agent/"

# postinst/prerm: manage the ONE leak, $PREFIX/bin/hermes, idempotently.
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
# Create $PREFIX/bin/hermes -> lib/hermes-agent/bin/hermes if missing (idempotent).
LINK="$PREFIX/bin/hermes"
TARGET="../lib/hermes-agent/bin/hermes"
mkdir -p "$PREFIX/bin"
if [ ! -e "$LINK" ] && [ ! -L "$LINK" ]; then
    ln -s "$TARGET" "$LINK"
fi
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
