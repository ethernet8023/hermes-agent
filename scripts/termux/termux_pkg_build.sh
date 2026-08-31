#!/usr/bin/env bash
# Shared build driver for pinned termux-packages recipes (python, nodejs-lts).
#
# Usage: termux_pkg_build.sh <recipe> <subdir> <version-key-path> <probe-script>
#   <recipe>            termux-packages package name, e.g. python | nodejs-lts
#   <subdir>            payload subdir the build installs into, e.g. python | node
#   <version-key-path>  jq path into pins.json holding the pinned version,
#                       e.g. .python.version | .node.version
#   <probe-script>      shell snippet (single-quoted, run with $BIN as the
#                       package's main binary path) that prints the built
#                       version, e.g. '"$BIN" --version 2>/dev/null | sed s/^v//'
#
# Cacheable: if <payload>/<subdir>/<main binary> exists and reports the pinned
# version, the build is skipped. Runs entirely inside the digest-pinned
# termux-docker container (docker must be available and arm64-capable).
#
# Cache safety: a FAILED or empty version probe of an existing cached tree is
# treated as a transient failure (binary busy, partial copy, etc.) — the cache
# is NOT destroyed. We abort loudly instead. Only a probe that SUCCEEDS and
# mismatches the pin means the cache is genuinely stale and safe to replace.
set -euo pipefail

RECIPE="$1"
SUBDIR="$2"
VERSION_KEY="$3"
PROBE="$4"
# Main binary whose existence+version decides the cache verdict, keyed by recipe.
case "$RECIPE" in
  python)     MAINBIN=bin/python ;;
  nodejs-lts) MAINBIN=bin/node ;;
  *) echo "termux_pkg_build: unknown recipe '$RECIPE' (add its main binary here)" >&2; exit 1 ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
PINS="$HERE/pins.json"
mkdir -p "$5"
PAYLOAD="$(cd "$5" && pwd)"
WORK="${6:-$(dirname "$PAYLOAD")/.termux-build}"

for tool in jq docker git; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

REPO=$(jq -r .termuxPackages.repo "$PINS")
COMMIT=$(jq -r .termuxPackages.commit "$PINS")
VER=$(jq -r "$VERSION_KEY" "$PINS")
DIGEST=$(jq -r .termuxDocker.digest "$PINS")
IMAGE="termux/termux-docker@$DIGEST"

# --- cache check (destroy only on a PROVEN stale cache) ----------------------
BIN="$PAYLOAD/$SUBDIR/$MAINBIN"
if [ -e "$BIN" ]; then
  GOT=$(BIN="$BIN" bash -c "$PROBE" 2>/dev/null || true)
  if [ "$GOT" = "$VER" ]; then
    echo "termux_pkg_build($RECIPE): cache hit — $PAYLOAD/$SUBDIR already has $RECIPE $VER"
    exit 0
  fi
  if [ -z "$GOT" ]; then
    echo "termux_pkg_build($RECIPE): version probe of cached $BIN FAILED (empty output)." >&2
    echo "  Refusing to rm -rf a possibly-good cache on a transient probe failure — aborting." >&2
    echo "  Fix the environment (e.g. binary busy/arch mismatch) or remove $PAYLOAD/$SUBDIR by hand to force a rebuild." >&2
    exit 1
  fi
  echo "termux_pkg_build($RECIPE): stale cache (found $GOT, pin is $VER) — rebuilding"
  rm -rf "$PAYLOAD/$SUBDIR"
fi

# --- environment gate --------------------------------------------------------
[ "$(uname -m)" = "aarch64" ] || { echo "host is not aarch64: $(uname -m)" >&2; exit 1; }
docker run --rm --platform linux/arm64 "$IMAGE" uname -m | grep -qx aarch64 \
  || { echo "container is not aarch64" >&2; exit 1; }

# --- clone recipe at the pinned commit (shallow) -----------------------------
mkdir -p "$WORK"
cd "$WORK"
if [ -d termux-packages/.git ]; then
  git -C termux-packages fetch --depth 1 origin "$COMMIT"
  git -C termux-packages checkout --force "$COMMIT"
else
  git clone --depth 1 "$REPO" termux-packages
  git -C termux-packages fetch --depth 1 origin "$COMMIT"
  git -C termux-packages checkout --force "$COMMIT"
fi
[ "$(git -C termux-packages rev-parse HEAD)" = "$COMMIT" ] \
  || { echo "clone is not at pinned commit" >&2; exit 1; }

# --- build inside the container ----------------------------------------------
# Consume the recipe's own build machinery (its patches are what we want);
# install into a prefix that lands at <payload>/<subdir>/ instead of $PREFIX.
# Serial build: CPython/node are ~30min at -j1, and the arm runner is OOM-sensitive.
mkdir -p "$PAYLOAD"
docker run --rm --platform linux/arm64 \
  -v "$WORK/termux-packages:/root/termux-packages" \
  -v "$PAYLOAD:/payload" \
  "$IMAGE" bash -c '
    set -euo pipefail
    cd /root/termux-packages
    export TERMUX_ARCH=aarch64
    export TERMUX_PREFIX=/payload/'"$SUBDIR"'
    export TERMUX_MAKE_PROCESSES=1
    export MAKEFLAGS=-j1
    ./scripts/run-docker.sh ./build-package.sh -a aarch64 -f '"$RECIPE"'
    # ./build-package.sh installs into its own $TERMUX_PREFIX tree; copy the
    # staged prefix out to /payload/'"$SUBDIR"'.
    if [ -d /data/data/com.termux/files/usr ]; then
      rm -rf /payload/'"$SUBDIR"'
      cp -a /data/data/com.termux/files/usr /payload/'"$SUBDIR"'
    fi
    BIN=/payload/'"$SUBDIR"'/'"$MAINBIN"'
    '"$PROBE"'
  ' | { read -r BUILT; [ "$BUILT" = "$VER" ] || { echo "termux_pkg_build($RECIPE): built version '$BUILT' != pin '$VER'" >&2; exit 1; }; }

echo "termux_pkg_build($RECIPE): built $RECIPE $VER at $PAYLOAD/$SUBDIR"
