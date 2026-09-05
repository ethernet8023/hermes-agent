#!/usr/bin/env bash
# Build + push the derived termux builder image (toolchain pre-baked).
#
# The image is content-addressed off the pinned base: its tag IS the
# pm/lock.json termux-docker digest (short form), so a lock bump produces
# a new builder image and the old one is never reused against a new base.
# Pushes to GHCR with the repo's CI identity (GITHUB_TOKEN); idempotent --
# an existing identical tag is left alone.
#
# Usage: build_builder_image.sh            (build + push if missing)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
cd "$REPO_ROOT"

DIGEST="$(python3 - <<'PYD'
import sys
sys.path.insert(0, ".")
from pm.lock import Lockfile
from pm.paths import lockfile_path
print(Lockfile(lockfile_path()).version("termux-docker"))
PYD
)" || { echo "failed to read the termux-docker digest from pm/lock.json" >&2; exit 1; }
[ -n "$DIGEST" ] || { echo "termux-docker digest missing" >&2; exit 1; }

BASE="termux/termux-docker@${DIGEST}"
SHORT="${DIGEST#sha256:}"
SHORT="${SHORT:0:12}"
REGISTRY="ghcr.io"
OWNER="${GITHUB_REPOSITORY_OWNER,,}"
IMAGE="${REGISTRY}/${OWNER}/hermes-termux-builder:${SHORT}"

if docker manifest inspect "$IMAGE" >/dev/null 2>&1; then
    echo "builder image already published: $IMAGE"
    echo "$IMAGE"
    exit 0
fi

echo "building $IMAGE from $BASE"
docker build \
    -f scripts/termux/termux-builder.Dockerfile \
    --build-arg "BASE=${BASE}" \
    -t "$IMAGE" \
    scripts/termux \
    || { echo "builder image build failed" >&2; exit 1; }

# Smoke: the baked image must answer the runtime probes termux_build.sh
# performs (clang + rustc + cargo + make + /bin/sh) before we publish it.
docker run --rm --platform linux/arm64 --user root "$IMAGE" \
    /data/data/com.termux/files/usr/bin/bash -c '
        export PREFIX=/data/data/com.termux/files/usr
        for tool in clang rustc cargo make; do
            command -v "$tool" >/dev/null 2>&1 || { echo "smoke FAIL: $tool"; exit 1; }
        done
        [ -e /bin/sh ] || { echo "smoke FAIL: /bin/sh"; exit 1; }
        echo "builder image smoke OK"
    ' || { echo "builder image failed its smoke test" >&2; exit 1; }

echo "$GITHUB_TOKEN" | docker login "$REGISTRY" -u "${GITHUB_ACTOR:-x}" --password-stdin
docker push "$IMAGE" || { echo "builder image push failed" >&2; exit 1; }
echo "published $IMAGE"
echo "$IMAGE"
