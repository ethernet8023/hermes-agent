#!/usr/bin/env bash
# Build node (nodejs-lts recipe) from the pinned termux-packages commit into
# <payload>/node/. Thin wrapper around the shared termux_pkg_build.sh — see it
# for the caching, pinning, and container discipline this inherits.
#
# Usage: build_node.sh <payload-dir> [workdir]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec "$HERE/termux_pkg_build.sh" nodejs-lts node .node.version \
  '"$BIN" --version 2>/dev/null | sed "s/^v//"' "$@"
