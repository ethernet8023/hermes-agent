#!/usr/bin/env bash
# Build node from the pinned termux-packages nodejs-lts recipe into <payload>/node/.
#
# Usage: build_node.sh <payload-dir> [workdir]
#
# Cacheable: if <payload>/node/bin/node exists and reports the pinned version,
# the build is skipped. Same container/pin discipline as build_cpython.sh.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PINS="$HERE/pins.json"
PAYLOAD="$(cd "$1" && pwd)"
WORK="${2:-$(dirname "$PAYLOAD")/.termux-build}"

for tool in jq docker; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

REPO=$(jq -r .termuxPackages.repo "$PINS")
COMMIT=$(jq -r .termuxPackages.commit "$PINS")
NODEVER=$(jq -r .node.version "$PINS")
DIGEST=$(jq -r .termuxDocker.digest "$PINS")
IMAGE="termux/termux-docker@$DIGEST"

# --- cache check -------------------------------------------------------------
if [ -x "$PAYLOAD/node/bin/node" ]; then
  GOT=$("$PAYLOAD/node/bin/node" --version 2>/dev/null | sed 's/^v//' || true)
  if [ "$GOT" = "$NODEVER" ]; then
    echo "build_node: cache hit — $PAYLOAD/node already has node $NODEVER"
    exit 0
  fi
  echo "build_node: stale cache (found $GOT, pin is $NODEVER) — rebuilding"
  rm -rf "$PAYLOAD/node"
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
# node is a pure-C++ build (no rust), but still serial per the plan's OOM stance.
mkdir -p "$PAYLOAD"
docker run --rm --platform linux/arm64 \
  -v "$WORK/termux-packages:/root/termux-packages" \
  -v "$PAYLOAD:/payload" \
  "$IMAGE" bash -lc '
    set -euo pipefail
    cd /root/termux-packages
    export TERMUX_ARCH=aarch64
    export TERMUX_PREFIX=/payload/node
    export TERMUX_MAKE_PROCESSES=1
    export MAKEFLAGS=-j1
    ./scripts/run-docker.sh ./build-package.sh -a aarch64 -f nodejs-lts
    if [ -d /data/data/com.termux/files/usr ]; then
      rm -rf /payload/node
      cp -a /data/data/com.termux/files/usr /payload/node
    fi
    "/payload/node/bin/node" --version | grep -qx "v'"$NODEVER"'" || {
      echo "node version mismatch: $("/payload/node/bin/node" --version) != v'"$NODEVER"'" >&2
      exit 1
    }
  '

echo "build_node: built node $NODEVER at $PAYLOAD/node"
