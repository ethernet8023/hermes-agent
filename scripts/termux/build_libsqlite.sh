#!/usr/bin/env bash
# Stage the pinned bionic libsqlite (pm package `libsqlite`, the termux-main
# libsqlite .deb) into <payload>/libsqlite/. Same pm-consumer shape as
# build_cpython/build_node/build_uv: pm owns the pin, the download, the
# hardened extraction, and the file-evidence verify. The payload python's
# sqlite3 module dlopens this lib at import time -- the sealed deb must
# carry it (the on-device termux tree may lack the libsqlite package).
#
# Usage: build_libsqlite.sh <payload-dir>
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec bash "$HERE/termux_pkg_build.sh" libsqlite libsqlite "$@"
