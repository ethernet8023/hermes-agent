#!/usr/bin/env bash
# ./pm — dev bootstrapper. Uses the pinned uv from pm/lock.json (installing
# it into the pm store if missing), then runs `python -m pm.cli` through it.
# No system python, no system uv, no PATH assumptions: the lockfile is the
# only authority. Usage: ./pm.sh <verb> [...]; plain `./pm.sh` = `develop`
# (install everything + venv, then drop into an activated subshell).
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lock="$repo/pm/lock.json"

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=darwin ;;
  MINGW*|MSYS*|CYGWIN*) os=win32 ;;
  *) echo "pm: unsupported OS $(uname -s)" >&2; exit 1 ;;
esac
if [ "$os" = win32 ]; then
  # PROCESSOR_ARCHITECTURE lies under an emulated shell (x64 msys on a
  # WoA box reports AMD64); the registry carries the machine's truth.
  winarch="$(reg.exe query 'HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment' /v PROCESSOR_ARCHITECTURE 2>/dev/null | tr -d '\r' | awk '/PROCESSOR_ARCHITECTURE/ {print $NF}')"
  case "${winarch:-${PROCESSOR_ARCHITECTURE:-}}" in
    ARM64) arch=arm64 ;;
    *) arch=x64 ;;
  esac
else
  case "$(uname -m)" in
    arm64|aarch64) arch=arm64 ;;
    x86_64|amd64) arch=x64 ;;
    *) echo "pm: unsupported arch $(uname -m)" >&2; exit 1 ;;
  esac
fi
target="$os-$arch"

# lock.json is machine-written (sorted keys, 2-space indent): read the uv
# pin's version + this target's url/sha256 with awk — no python yet.
pin() { # $1 = field (url | sha256)
  awk -v target="$target" -v field="$1" '
    /^    "uv": \{/ { in_uv = 1 }
    in_uv && $0 ~ "^        \"" target "\": \\{" { in_t = 1 }
    in_t && $0 ~ "^          \"" field "\":" {
      gsub(/.*: "|",?$/, ""); print; exit
    }' "$lock"
}
uv_version="$(awk '
  /^    "uv": \{/ { in_uv = 1 }
  in_uv && /^      "version":/ { gsub(/.*: "|",?$/, ""); print; exit }' "$lock")"
py_version="$(awk '
  /^    "python": \{/ { in_py = 1 }
  in_py && /^      "version":/ { gsub(/.*: "|"$|",$/, ""); print; exit }' "$lock" \
  | cut -d+ -f1 | cut -d. -f1,2)"
[ -n "$uv_version" ] || { echo "pm: no uv pin in pm/lock.json" >&2; exit 1; }

store="${HERMES_RUNTIME_DIR:-$HOME/.hermes/tools}"
entry="$store/uv-$uv_version-$target"
uv="$entry/uv"; [ "$os" = win32 ] && uv="$entry/uv.exe"

if [ ! -x "$uv" ]; then
  url="$(pin url)"; sha="$(pin sha256)"
  [ -n "$url" ] && [ -n "$sha" ] || { echo "pm: no uv artifact for $target" >&2; exit 1; }
  echo "pm: fetching pinned uv $uv_version ($target)" >&2
  mkdir -p "$store"
  tmp="$(mktemp -d "$store/.bootstrap-XXXXXX")"; trap 'rm -rf "$tmp"' EXIT
  archive="$tmp/${url##*/}"
  curl -fsSL -o "$archive" "$url"
  got="$( (sha256sum "$archive" 2>/dev/null || shasum -a 256 "$archive") | cut -d' ' -f1 | tr -d '\\')"
  [ "$got" = "$sha" ] || { echo "pm: sha256 mismatch for uv (got $got, pinned $sha)" >&2; exit 1; }
  mkdir -p "$tmp/tree"
  case "$archive" in
    *.zip) unzip -q "$archive" -d "$tmp/tree" ;;
    *) tar -xzf "$archive" -C "$tmp/tree" ;;
  esac
  # flatten a single wrapping dir (uv tarballs ship uv-<triple>/uv)
  inner="$(find "$tmp/tree" -mindepth 1 -maxdepth 1)"
  if [ "$(printf '%s\n' "$inner" | wc -l)" = 1 ] && [ -d "$inner" ]; then
    mv "$inner" "$tmp/entry"
  else
    mv "$tmp/tree" "$tmp/entry"
  fi
  mkdir -p "$store"
  rm -rf "$entry"
  mv "$tmp/entry" "$entry"
fi

cd "$repo"
exec "$uv" run --no-project --python "${py_version:-3.11}" python -m pm.cli "${@:-develop}"
