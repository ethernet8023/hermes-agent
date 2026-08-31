#!/usr/bin/env bash
# Stage the pinned termux CPython .deb into <payload>/python/.
# Thin wrapper around the shared termux_pkg_build.sh — see it for the
# caching, pinning, and extract discipline this inherits.
#
# Usage: build_cpython.sh <payload-dir> [workdir]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec bash "$HERE/termux_pkg_build.sh" python python "$@"
