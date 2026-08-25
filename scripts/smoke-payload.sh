#!/bin/bash
# Smoke-test a staged payload exactly the way the desktop spawns it:
# the payload venv's interpreter, cwd at the staged repo, no PYTHONPATH,
# no network, lazy installs off. Usage: smoke-payload.sh <payload-dir>
set -e
PAYLOAD="${1:?usage: smoke-payload.sh <payload-dir>}"
cd "$PAYLOAD"

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) PY="$PWD/venv/Scripts/python.exe" ;;
  *)                    PY="$PWD/venv/bin/python" ;;
esac

[ -f "$PY" ] || { echo "FAIL: no venv interpreter at $PY"; exit 1; }
[ -f manifest.json ] || { echo "FAIL: no manifest.json"; exit 1; }
[ -f tools/facts.json ] || { echo "FAIL: no tools/facts.json"; exit 1; }

TOOLS="$PWD/tools"
# native python must see a native path, not an MSYS one
command -v cygpath >/dev/null 2>&1 && TOOLS="$(cygpath -w "$TOOLS")"
# pm prints UTF-8 (✓/✗); Windows consoles default to cp1252
export PYTHONUTF8=1
cd hermes-agent

echo "— python boots + hermes_cli imports out of the payload —"
env -u PYTHONPATH -u PYTHONHOME \
  HERMES_RUNTIME_DIR="$TOOLS" HERMES_DISABLE_LAZY_INSTALLS=1 \
  "$PY" -c "import hermes_cli.config; import pm; print('imports ok')"

echo "— hermes --version through the real entrypoint —"
env -u PYTHONPATH -u PYTHONHOME \
  HERMES_RUNTIME_DIR="$TOOLS" HERMES_DISABLE_LAZY_INSTALLS=1 \
  "$PY" -m hermes_cli.main --version

echo "— pm sees the staged tools —"
env -u PYTHONPATH -u PYTHONHOME \
  HERMES_RUNTIME_DIR="$TOOLS" HERMES_DISABLE_LAZY_INSTALLS=1 \
  "$PY" -m pm.cli doctor

echo "SMOKE OK"
