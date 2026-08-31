#!/usr/bin/env bash
# Build CPython from the pinned termux-packages recipe into <payload>/python/.
# Thin wrapper around the shared termux_pkg_build.sh — see it for the
# caching, pinning, and container discipline this inherits.
#
# Usage: build_cpython.sh <payload-dir> [workdir]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec "$HERE/termux_pkg_build.sh" python python .python.version \
  '"$BIN" -c "import platform; print(platform.python_version())"' "$@"
