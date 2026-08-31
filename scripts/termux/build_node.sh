#!/usr/bin/env bash
# Stage the pinned termux nodejs-lts .deb into <payload>/node/.
# Thin wrapper around the shared termux_pkg_build.sh — see it for the
# caching, pinning, and extract discipline this inherits.
#
# Usage: build_node.sh <payload-dir> [workdir]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec bash "$HERE/termux_pkg_build.sh" nodejs-lts node "$@"
