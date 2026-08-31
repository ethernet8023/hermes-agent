#!/usr/bin/env bash
# Build CPython from the pinned termux-packages recipe into <payload>/python/.
#
# Usage: build_cpython.sh <payload-dir> [workdir]
#   <payload-dir>  where python/ lands (same payload root the .deb is assembled from)
#   [workdir]      scratch dir for the termux-packages clone (default: <payload>/../.termux-build)
#
# Cacheable: if <payload>/python/bin/python exists and reports the pinned version,
# the build is skipped. Runs entirely inside the digest-pinned termux-docker
# container (docker must be available and arm64-capable).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PINS="$HERE/pins.json"
PAYLOAD="$(cd "$1" && pwd)"
WORK="${2:-$(dirname "$PAYLOAD")/.termux-build}"

for tool in jq docker python3 git; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

REPO=$(jq -r .termuxPackages.repo "$PINS")
COMMIT=$(jq -r .termuxPackages.commit "$PINS")
PYVER=$(jq -r .python.version "$PINS")
DIGEST=$(jq -r .termuxDocker.digest "$PINS")
IMAGE="termux/termux-docker@$DIGEST"

# --- cache check -------------------------------------------------------------
if [ -x "$PAYLOAD/python/bin/python" ]; then
  GOT=$("$PAYLOAD/python/bin/python" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)
  if [ "$GOT" = "$PYVER" ]; then
    echo "build_cpython: cache hit — $PAYLOAD/python already has CPython $PYVER"
    exit 0
  fi
  echo "build_cpython: stale cache (found $GOT, pin is $PYVER) — rebuilding"
  rm -rf "$PAYLOAD/python"
fi

# --- environment gate (mirror Task 2's gate) --------------------------------
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
# install into a prefix that lands at <payload>/python/ instead of $PREFIX.
# Serial build: CPython is ~30min at -j1, and the arm runner is OOM-sensitive.
mkdir -p "$PAYLOAD"
docker run --rm --platform linux/arm64 \
  -v "$WORK/termux-packages:/root/termux-packages" \
  -v "$PAYLOAD:/payload" \
  "$IMAGE" bash -lc '
    set -euo pipefail
    cd /root/termux-packages
    export TERMUX_ARCH=aarch64
    export TERMUX_PREFIX=/payload/python
    export TERMUX_MAKE_PROCESSES=1
    export MAKEFLAGS=-j1
    ./scripts/run-docker.sh ./build-package.sh -a aarch64 -f python
    # ./build-package.sh installs into its own $TERMUX_PREFIX tree; copy the
    # staged prefix out to /payload/python.
    if [ -d /data/data/com.termux/files/usr ]; then
      rm -rf /payload/python
      cp -a /data/data/com.termux/files/usr /payload/python
    fi
    /payload/python/bin/python -c "import platform; assert platform.python_version() == \"'"$PYVER"'\", platform.python_version()"
  '

echo "build_cpython: built CPython $PYVER at $PAYLOAD/python"
