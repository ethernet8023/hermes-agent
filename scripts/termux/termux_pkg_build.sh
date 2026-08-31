#!/usr/bin/env bash
# Stage a pinned Termux-distribution interpreter/runtime into the payload.
#
# python and node are NOT built from the termux-packages recipes here:
# the upstream package-builder image that runs those recipes is amd64-only,
# so building them on the arm64 lane would need QEMU (rejected) or building
# the whole builder image ourselves. Instead we consume Termux's own
# CI-built .debs at an EXACT pin (url + sha256 in pins.json) — the same
# pin-table discipline as every other third-party artifact. The payload
# carries its own copy under lib/hermes-agent/, so the phone's installed
# python/node are untouched; we still pick the exact version.
#
# Usage: termux_pkg_build.sh <recipe> <subdir> <payload-dir> [workdir]
#   <recipe>      termux package name: python | nodejs-lts
#   <subdir>      payload subdir the runtime lands in: python | node
#   <payload-dir> payload root (staged runtime lands in <payload>/<subdir>)
#   [workdir]     scratch dir for the .deb download (default: alongside payload)
#
# Cacheable: a previously staged <payload>/<subdir> is a cache hit when its
# .termux-stamp (recipe + debVersion, written at stage time) matches the
# current pins — file evidence, NOT executing the staged binary: the staged
# binaries are bionic/arm64 and cannot run on this host.
#
# Cache safety: a missing/unreadable stamp is a transient-looking failure —
# we do NOT destroy the staging, we abort loudly. Hand-remove the subdir to
# force a re-stage.
#
# The .deb extracts on any host: dpkg-deb -x when available, else the
# stdlib ar+tar fallback (scripts/termux/extract_deb.py).
set -euo pipefail

RECIPE="$1"
SUBDIR="$2"
# Main binary whose presence anchors the staging, keyed by recipe.
# Termux .debs carry the full $PREFIX path in data.tar: files land at
# <subdir>/data/data/com.termux/files/usr/... — the tree keeps its real
# on-device shape and the payload mounts it back at the same prefix.
PREFIX_REL="data/data/com.termux/files/usr"
case "$RECIPE" in
  python)     MAINBIN="$PREFIX_REL/bin/python3.14" ;;
  nodejs-lts) MAINBIN="$PREFIX_REL/bin/node" ;;
  *) echo "termux_pkg_build: unknown recipe '$RECIPE' (add its main binary here)" >&2; exit 1 ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
PINS="$HERE/pins.json"
mkdir -p "$3"
PAYLOAD="$(cd "$3" && pwd)"
WORK="${4:-$(dirname "$PAYLOAD")/.termux-build}"
STAMP="$PAYLOAD/$SUBDIR/.termux-stamp"
STAMP_WANT="$RECIPE $(jq -r --arg r "$RECIPE" '.[$r].debVersion' "$PINS")"

for tool in jq curl; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

# --- cache check (destroy only on a PROVEN stale staging) ---------------------
if [ -e "$PAYLOAD/$SUBDIR/$MAINBIN" ]; then
  if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$STAMP_WANT" ]; then
    echo "termux_pkg_build($RECIPE): cache hit — $PAYLOAD/$SUBDIR already has $STAMP_WANT"
    exit 0
  fi
  if [ ! -f "$STAMP" ]; then
    echo "termux_pkg_build($RECIPE): staged tree exists but stamp is missing — refusing to" >&2
    echo "  remove a possibly-good staging on missing evidence. Remove $PAYLOAD/$SUBDIR by" >&2
    echo "  hand to force a re-stage." >&2
    exit 1
  fi
  echo "termux_pkg_build($RECIPE): stale staging (stamp '$(cat "$STAMP")', pin is '$STAMP_WANT') — re-staging"
  rm -rf "$PAYLOAD/$SUBDIR"
fi

DEB_URL=$(jq -r --arg r "$RECIPE" '.[$r].debUrl' "$PINS")
DEB_SHA=$(jq -r --arg r "$RECIPE" '.[$r].debSha256' "$PINS")
DEB_PATH="$WORK/$(basename "$DEB_URL")"
mkdir -p "$WORK"

# --- download + digest-verify the pinned .deb (no salvage) --------------------
if [ ! -f "$DEB_PATH" ] || ! printf '%s  %s\n' "$DEB_SHA" "$DEB_PATH" | sha256sum -c - >/dev/null 2>&1; then
  echo "termux_pkg_build($RECIPE): downloading $DEB_URL"
  curl -fL --retry 6 --retry-all-errors --connect-timeout 20 -o "$DEB_PATH" "$DEB_URL"
fi
printf '%s  %s\n' "$DEB_SHA" "$DEB_PATH" | sha256sum -c - >/dev/null \
  || { echo "termux_pkg_build($RECIPE): sha256 mismatch for $DEB_PATH" >&2; exit 1; }

# --- verify the .deb's control stanza matches the pin (file evidence) ----------
GOT=$(python3 "$HERE/deb_control_version.py" --deb "$DEB_PATH" --package "$RECIPE") \
  || { echo "termux_pkg_build($RECIPE): failed to read control stanza from $DEB_PATH" >&2; exit 1; }
WANT=$(jq -r --arg r "$RECIPE" '.[$r].debVersion' "$PINS")
[ "$GOT" = "$WANT" ] \
  || { echo "termux_pkg_build($RECIPE): .deb control version '$GOT' != pin '$WANT'" >&2; exit 1; }

# --- extract into the payload -------------------------------------------------
# The .deb installs into termux's $PREFIX on a phone; extracting stages the
# same tree at <payload>/<subdir>. The baked $PREFIX paths inside are correct
# on-device because Termux's $PREFIX is contractual.
rm -rf "$PAYLOAD/$SUBDIR"
if command -v dpkg-deb >/dev/null 2>&1; then
  dpkg-deb -x "$DEB_PATH" "$PAYLOAD/$SUBDIR.tmp"
else
  python3 "$HERE/extract_deb.py" --deb "$DEB_PATH" --out "$PAYLOAD/$SUBDIR.tmp"
fi
printf '%s\n' "$STAMP_WANT" > "$PAYLOAD/$SUBDIR.tmp/.termux-stamp"
mv "$PAYLOAD/$SUBDIR.tmp" "$PAYLOAD/$SUBDIR"

[ -e "$PAYLOAD/$SUBDIR/$MAINBIN" ] \
  || { echo "termux_pkg_build($RECIPE): staged tree lacks $MAINBIN — bad .deb?" >&2; exit 1; }

echo "termux_pkg_build($RECIPE): staged $STAMP_WANT at $PAYLOAD/$SUBDIR (from $(basename "$DEB_PATH"))"
