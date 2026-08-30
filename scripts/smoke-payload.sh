#!/bin/bash
# Smoke-test a staged payload the way the DESKTOP spawns it (the self-
# relative CLI shim, which sets its own PYTHONPATH) plus the raw store
# python with an explicit PYTHONPATH (the legacy spawn). cwd at the staged
# repo, no network, lazy installs off. Usage: smoke-payload.sh <payload-dir>
set -e
PAYLOAD="${1:?usage: smoke-payload.sh <payload-dir>}"
cd "$PAYLOAD"

PYTHON_ENTRY=$(node -e "
  const f = require(process.argv[1] + '/tools/facts.json');
  console.log(f.packages.python.entry);
" "$PWD")
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) PY="$PWD/tools/$PYTHON_ENTRY/python.exe"; SP="$PWD/venv/Lib/site-packages" ;;
  *)                    PY="$PWD/tools/$PYTHON_ENTRY/bin/python3"; SP="$PWD/venv/lib/python3.11/site-packages" ;;
esac

[ -f "$PY" ] || { echo "FAIL: no store interpreter at $PY"; exit 1; }
[ -d "$SP" ] || { echo "FAIL: no venv site-packages at $SP"; exit 1; }
[ -f manifest.json ] || { echo "FAIL: no manifest.json"; exit 1; }
[ -f tools/facts.json ] || { echo "FAIL: no tools/facts.json"; exit 1; }

TOOLS="$PWD/tools"
# native python must see a native path, not an MSYS one
command -v cygpath >/dev/null 2>&1 && TOOLS="$(cygpath -w "$TOOLS")" && SP="$(cygpath -w "$SP")"
# pm prints UTF-8 (✓/✗); Windows consoles default to cp1252
export PYTHONUTF8=1
cd hermes-agent

echo "— store python boots + hermes_cli imports out of the payload —"
env -u PYTHONPATH -u PYTHONHOME \
  HERMES_RUNTIME_DIR="$TOOLS" HERMES_DISABLE_LAZY_INSTALLS=1 \
  PYTHONPATH="$SP" \
  "$PY" -c "import hermes_cli.config; import pm; print('imports ok')"

echo "— hermes --version through the real entrypoint —"
env -u PYTHONPATH -u PYTHONHOME \
  HERMES_RUNTIME_DIR="$TOOLS" HERMES_DISABLE_LAZY_INSTALLS=1 \
  PYTHONPATH="$SP" \
  "$PY" -m hermes_cli.main --version

echo "— the self-relative CLI shim boots with NO PYTHONPATH (it sets its own) —"
SHIM="../bin/hermes$(case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) echo .exe ;; esac)"
env -u PYTHONPATH -u PYTHONHOME \
  HERMES_RUNTIME_DIR="$TOOLS" HERMES_DISABLE_LAZY_INSTALLS=1 \
  "$SHIM" --version

echo "— pm sees the staged tools —"
env -u PYTHONPATH -u PYTHONHOME \
  HERMES_RUNTIME_DIR="$TOOLS" HERMES_DISABLE_LAZY_INSTALLS=1 \
  PYTHONPATH="$SP" \
  "$PY" -m pm.cli doctor

echo "SMOKE OK"
