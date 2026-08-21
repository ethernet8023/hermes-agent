#!/usr/bin/env bash
# Bundled desktop install+update E2E driver.
#
# Installs a bundled desktop app from the PREV tag's installer, then
# updates it to HEAD through the app's own electron-updater "Update now"
# button.  The bundles are pre-built by install-e2e-bundled-build.yml and
# downloaded as workflow artifacts.
#
# This driver is OS-agnostic: it stages a bare-clone git redirect (like
# installer-script-e2e.sh), installs the prev-tag's bundled installer
# (NSIS on Windows, DMG on macOS, AppImage on Linux), then serves the
# HEAD bundle's latest*.yml + installer as a local HTTP feed so the
# app's electron-updater finds the update and downloads it.
#
# The actual GUI driving (clicking "Update now") reuses the existing
# drive-update.cjs / launch-from-spec.mjs Playwright drivers from the
# existing desktop E2E infrastructure, because the update flow is
# identical once the app is installed — only the install source differs.
#
# Usage:
#   bundled-e2e.sh --os windows|macos|linux \
#     --prev-tag v2026.8.13 --head-sha abc123 \
#     --prev-bundles <dir> --head-bundles <dir>

set -euo pipefail

OS=""
PREV_TAG=""
HEAD_SHA=""
PREV_BUNDLES=""
HEAD_BUNDLES=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --os) OS="$2"; shift 2 ;;
    --prev-tag) PREV_TAG="$2"; shift 2 ;;
    --head-sha) HEAD_SHA="$2"; shift 2 ;;
    --prev-bundles) PREV_BUNDLES="$2"; shift 2 ;;
    --head-bundles) HEAD_BUNDLES="$2"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; exit 1 ;;
  esac
done

[ -n "$OS" ] || { echo "error: --os is required" >&2; exit 1; }
[ -n "$PREV_TAG" ] || { echo "error: --prev-tag is required" >&2; exit 1; }
[ -n "$HEAD_SHA" ] || { echo "error: --head-sha is required" >&2; exit 1; }
[ -n "$PREV_BUNDLES" ] || { echo "error: --prev-bundles is required" >&2; exit 1; }
[ -n "$HEAD_BUNDLES" ] || { echo "error: --head-bundles is required" >&2; exit 1; }

# ── Resolve paths ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WORK_ROOT="${HERMES_E2E_WORKROOT:-/tmp/hermes-bundled-e2e}"
LOG_DIR="${HERMES_E2E_LOG_DIR:-$WORK_ROOT/logs}"
INSTALL_DIR="$WORK_ROOT/hermes-install"
HERMES_HOME="$WORK_ROOT/hermes-home"
SERVE_REPO="$WORK_ROOT/serve.git"
STATE_FILE="$WORK_ROOT/state.env"

mkdir -p "$LOG_DIR" "$WORK_ROOT" "$HERMES_HOME"

# ── Helpers ──────────────────────────────────────────────────────────
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG_DIR/driver.log"; }
fail() { log "FAIL: $*"; exit 1; }

# ── Phase 1: Stage the serve repo (main -> prev-tag) ─────────────────
phase_stage() {
  log "STAGE: bare serve repo, main -> $PREV_TAG"

  rm -rf "$WORK_ROOT" 2>/dev/null || true
  mkdir -p "$WORK_ROOT" "$HERMES_HOME" "$LOG_DIR"

  local old_sha
  old_sha="$(git -C "$REPO_ROOT" rev-parse "$PREV_TAG^{commit}")"
  local current_sha
  current_sha="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  log "  HEAD (update target): $current_sha"
  log "  PREV ($PREV_TAG): $old_sha"
  [ "$old_sha" != "$current_sha" ] || fail "PREV differs from HEAD (an update is genuinely available)"

  # Bare-clone the checkout: this is the repo the installer and updater
  # talk to (same isolation trick as installer-script-e2e.sh).
  git clone --bare --quiet "$REPO_ROOT" "$SERVE_REPO" 2>/dev/null
  git -C "$SERVE_REPO" update-ref refs/heads/main "$old_sha"
  git -C "$SERVE_REPO" symbolic-ref HEAD refs/heads/main
  git -C "$SERVE_REPO" config uploadpack.allowAnySHA1InWant true

  # Git URL redirect: serve.git insteadOf the canonical URLs.
  local gitconfig="$WORK_ROOT/git-redirect"
  cat > "$gitconfig" <<EOF
[url "file://$SERVE_REPO"]
  insteadOf = https://github.com/NousResearch/hermes-agent.git
  insteadOf = https://github.com/ethernet8023/hermes-agent.git
  insteadOf = git@github.com:NousResearch/hermes-agent.git
  insteadOf = git@github.com:ethernet8023/hermes-agent.git
EOF

  echo "OLD_SHA=$old_sha" > "$STATE_FILE"
  echo "OLD_REF=$PREV_TAG" >> "$STATE_FILE"
  echo "CURRENT_SHA=$current_sha" >> "$STATE_FILE"
  echo "GIT_CONFIG_GLOBAL=$gitconfig" >> "$STATE_FILE"

  log "  serve.git ready, main at $old_sha ($PREV_TAG)"
}

# ── Phase 2: Install the prev-tag bundled desktop app ────────────────
phase_install() {
  . "$STATE_FILE"
  export GIT_CONFIG_GLOBAL
  export HERMES_HOME

  case "$OS" in
    windows)
      phase_install_windows
      ;;
    macos)
      phase_install_macos
      ;;
    linux)
      phase_install_linux
      ;;
    *)
      fail "unknown OS: $OS"
      ;;
  esac

  # Verify the install landed at the prev-tag commit.
  local installed_sha
  installed_sha="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
  [ "$installed_sha" = "$OLD_SHA" ] || fail "installed checkout is $installed_sha, expected OLD ($OLD_SHA)"
  log "  install verified: checkout at $OLD_SHA ($OLD_REF)"

  # Verify hermes runs.
  local hermes_bin
  case "$OS" in
    windows) hermes_bin="$INSTALL_DIR/venv/Scripts/hermes.exe" ;;
    *) hermes_bin="$INSTALL_DIR/venv/bin/hermes" ;;
  esac
  [ -x "$hermes_bin" ] || fail "no hermes binary at $hermes_bin"
  "$hermes_bin" --version 2>&1 | tee -a "$LOG_DIR/version-prev.log"
  log "  hermes --version works after install"
}

# ── Windows: install from the prev-tag NSIS installer ────────────────
phase_install_windows() {
  log "INSTALL (Windows): prev-tag bundled NSIS installer"
  local setup_exe
  setup_exe="$(find "$PREV_BUNDLES" -name '*Setup*.exe' -o -name '*.exe' | head -1)"
  [ -n "$setup_exe" ] || fail "no .exe installer found in $PREV_BUNDLES"
  [ -f "$setup_exe" ] || fail "installer not found: $setup_exe"
  log "  installer: $setup_exe ($(du -h "$setup_exe" | cut -f1))"

  # The NSIS installer is silent-capable: /S runs it unattended.
  # HERMES_HOME is set so the install lands in our isolated workroot.
  mkdir -p "$INSTALL_DIR"
  "$setup_exe" /S /D="$INSTALL_DIR" 2>&1 | tee -a "$LOG_DIR/install.log" || true

  # The bundled installer creates a Hermes.exe in the install dir.
  [ -d "$INSTALL_DIR/.git" ] || fail "no checkout at $INSTALL_DIR after install"
  local desktop_exe
  desktop_exe="$(find "$INSTALL_DIR" -name 'Hermes.exe' -not -path '*/venv/*' | head -1)"
  [ -n "$desktop_exe" ] || fail "no Hermes.exe found after install"
  log "  desktop app: $desktop_exe"
  echo "DESKTOP_EXE=$desktop_exe" >> "$STATE_FILE"
}

# ── macOS: install from the prev-tag DMG ─────────────────────────────
phase_install_macos() {
  log "INSTALL (macOS): prev-tag bundled DMG"
  local dmg
  dmg="$(find "$PREV_BUNDLES" -name '*.dmg' | head -1)"
  [ -n "$dmg" ] || fail "no .dmg found in $PREV_BUNDLES"
  [ -f "$dmg" ] || fail "dmg not found: $dmg"
  log "  dmg: $dmg ($(du -h "$dmg" | cut -f1))"

  xattr -dr com.apple.quarantine "$dmg" 2>/dev/null || true

  local mount
  mount="$(hdiutil attach -nobrowse -readonly "$dmg" | awk -F'\t' '/\/Volumes\//{print $NF; exit}')"
  [ -n "$mount" ] || fail "hdiutil attach produced no mount point"
  log "  dmg mounted at $mount"

  local app
  app="$(find "$mount" -maxdepth 1 -name '*.app' | head -1)"
  [ -n "$app" ] || { hdiutil detach "$mount" >/dev/null 2>&1 || true; fail "no .app inside the dmg"; }
  log "  app: $app"

  # Run the installer binary directly (same as macos-desktop-e2e.sh).
  local app_bin
  app_bin="$(find "$app/Contents/MacOS" -type f -perm +111 | head -1)"
  [ -n "$app_bin" ] || fail "no executable inside $app/Contents/MacOS"

  "$app_bin" 2>&1 | tee -a "$LOG_DIR/install.log" || true
  hdiutil detach "$mount" >/dev/null 2>&1 || true

  [ -d "$INSTALL_DIR/.git" ] || fail "no checkout at $INSTALL_DIR after install"

  # Find the installed app.
  local installed_app
  for cand in \
    "$INSTALL_DIR/apps/desktop/release/mac-arm64/Hermes.app" \
    "$INSTALL_DIR/apps/desktop/release/mac/Hermes.app" \
    "/Applications/Hermes.app"; do
    [ -d "$cand" ] && { installed_app="$cand"; break; }
  done
  [ -n "$installed_app" ] || fail "no installed Hermes.app after install"
  log "  installed app: $installed_app"
  echo "DESKTOP_APP=$installed_app" >> "$STATE_FILE"
}

# ── Linux: install from the prev-tag AppImage ────────────────────────
phase_install_linux() {
  log "INSTALL (Linux): prev-tag bundled AppImage"
  local appimage
  appimage="$(find "$PREV_BUNDLES" -name '*.AppImage' | head -1)"
  [ -n "$appimage" ] || fail "no .AppImage found in $PREV_BUNDLES"
  [ -f "$appimage" ] || fail "AppImage not found: $appimage"
  log "  AppImage: $appimage ($(du -h "$appimage" | cut -f1))"

  # AppImages are self-contained and don't "install" in the traditional
  # sense.  The bundled installer on Linux is the NSIS-like flow via the
  # setup-hermes.sh script.  However, the AppImage IS the desktop app —
  # for E2E we run it directly and test the updater from there.
  chmod +x "$appimage"

  # The AppImage doesn't create a checkout.  The bundled installer on
  # Linux uses setup-hermes.sh which clones the repo.  We run the script
  # from the prev-tag's own copy.
  mkdir -p "$INSTALL_DIR"
  local installer_script
  installer_script="$REPO_ROOT/setup-hermes.sh"
  [ -f "$installer_script" ] || fail "no setup-hermes.sh at $installer_script"

  HOME="$HERMES_HOME" "$installer_script" --yes 2>&1 | tee -a "$LOG_DIR/install.log" || true

  [ -d "$INSTALL_DIR/.git" ] || fail "no checkout at $INSTALL_DIR after install"

  # The AppImage is the desktop app for update testing.
  echo "DESKTOP_APPIMAGE=$appimage" >> "$STATE_FILE"
}

# ── Phase 3: Update to HEAD via the app's Update button ──────────────
phase_update() {
  . "$STATE_FILE"
  export GIT_CONFIG_GLOBAL
  export HERMES_HOME

  log "UPDATE: advance serve.git main -> $CURRENT_SHA, click Update now"

  # Advance the served repo to HEAD so the update becomes available.
  git -C "$SERVE_REPO" update-ref refs/heads/main "$CURRENT_SHA"
  log "  serve.git main advanced to $CURRENT_SHA"

  # Serve the HEAD bundle's latest*.yml as the electron-updater feed.
  # The app's app-update.yml points at the GitHub releases feed; we
  # override it to a local HTTP server that serves the HEAD bundle.
  local feed_yml
  feed_yml="$(find "$HEAD_BUNDLES" -name 'latest*.yml' -o -name 'nightly*.yml' | head -1)"
  [ -n "$feed_yml" ] || fail "no latest*.yml or nightly*.yml found in $HEAD_BUNDLES"

  # Patch the feed to point the file URL at our local server.
  local served_feed="$WORK_ROOT/feed/latest.yml"
  mkdir -p "$(dirname "$served_feed")"

  # Start a simple HTTP server serving the HEAD bundles directory.
  python3 -m http.server 18080 --directory "$HEAD_BUNDLES" &
  local http_pid=$!
  log "  HTTP feed server started (pid $http_pid) serving $HEAD_BUNDLES on :18080"
  # Give it a moment to start.
  sleep 1

  # Rewrite the feed's file URLs to point at the local server.
  sed "s|/|http://localhost:18080/|g" "$feed_yml" > "$served_feed" 2>/dev/null || cp "$feed_yml" "$served_feed"
  # Also copy the feed to the HEAD bundles dir so the server serves it.
  cp "$served_feed" "$HEAD_BUNDLES/latest.yml" 2>/dev/null || true

  # Now drive the app to click "Update now".  This reuses the existing
  # Playwright driver infrastructure — the update flow is identical once
  # the app is installed; only the install source differs.
  local drive_result=0

  case "$OS" in
    windows)
      phase_update_windows || drive_result=$?
      ;;
    macos)
      phase_update_macos || drive_result=$?
      ;;
    linux)
      phase_update_linux || drive_result=$?
      ;;
  esac

  # Stop the HTTP server.
  kill $http_pid 2>/dev/null || true

  [ $drive_result -eq 0 ] || fail "update driver failed (exit $drive_result)"

  # Verify the update landed at HEAD.
  local updated_sha
  updated_sha="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
  [ "$updated_sha" = "$CURRENT_SHA" ] || fail "checkout is $updated_sha, expected HEAD ($CURRENT_SHA)"
  log "  update verified: checkout at $CURRENT_SHA (HEAD)"

  # Verify hermes still runs.
  local hermes_bin
  case "$OS" in
    windows) hermes_bin="$INSTALL_DIR/venv/Scripts/hermes.exe" ;;
    *) hermes_bin="$INSTALL_DIR/venv/bin/hermes" ;;
  esac
  "$hermes_bin" --version 2>&1 | tee -a "$LOG_DIR/version-head.log"
  log "  hermes --version works after update"
}

# ── Windows: drive the update via Playwright ─────────────────────────
phase_update_windows() {
  local desktop_exe="${DESKTOP_EXE:-}"
  [ -n "$desktop_exe" ] || fail "no DESKTOP_EXE in state"
  [ -f "$desktop_exe" ] || fail "desktop exe not found: $desktop_exe"

  # Find the managed Node (installed by the bundled installer).
  local node_exe
  for cand in \
    "$HERMES_HOME/node/node.exe" \
    "$HERMES_HOME/bin/node/node.exe" \
    "$INSTALL_DIR/node/node.exe"; do
    [ -f "$cand" ] && { node_exe="$cand"; break; }
  done
  node_exe="${node_exe:-$(which node 2>/dev/null || echo "")}"
  [ -n "$node_exe" ] || fail "no node.exe found"

  # Seed a provider so the update leg meets the ready app shell.
  local env_file="$HERMES_HOME/.env"
  if [ ! -f "$env_file" ] || ! grep -q "OPENROUTER_API_KEY" "$env_file" 2>/dev/null; then
    echo "OPENROUTER_API_KEY=sk-or-...-key" >> "$env_file"
  fi

  # Set up the Playwright driver in a scratch dir.
  local pw_dir="$WORK_ROOT/pw-driver"
  mkdir -p "$pw_dir"
  local npm_cli
  npm_cli="$(dirname "$node_exe")/node_modules/npm/bin/npm-cli.js"
  [ -f "$npm_cli" ] || npm_cli="$(which npm 2>/dev/null)"
  (cd "$pw_dir" && "$node_exe" "$npm_cli" install --no-save --no-audit --no-fund "@playwright/test@1.55.0" 2>&1 | tail -5)

  # Copy the drive-update driver.
  cp "$SCRIPT_DIR/e2e-assets/drive-update.cjs" "$pw_dir/" 2>/dev/null || true

  # Run the Playwright driver: launches the app, clicks Update now.
  local proof_dir="$WORK_ROOT/proof"
  mkdir -p "$proof_dir"
  (cd "$pw_dir" && "$node_exe" "$pw_dir/drive-update.cjs" "$desktop_exe" "$proof_dir" 2>&1 | tee -a "$LOG_DIR/update-drive.log")
}

# ── macOS: drive the update via Playwright ───────────────────────────
phase_update_macos() {
  local desktop_app="${DESKTOP_APP:-}"
  [ -n "$desktop_app" ] || fail "no DESKTOP_APP in state"
  [ -d "$desktop_app" ] || fail "desktop app not found: $desktop_app"

  local app_bin="$desktop_app/Contents/MacOS/Hermes"
  [ -f "$app_bin" ] || app_bin="$(find "$desktop_app/Contents/MacOS" -type f -perm +111 | head -1)"
  [ -f "$app_bin" ] || fail "no executable inside $desktop_app"

  # Seed a provider.
  local env_file="$HERMES_HOME/.env"
  if [ ! -f "$env_file" ] || ! grep -q "OPENROUTER_API_KEY" "$env_file" 2>/dev/null; then
    echo "OPENROUTER_API_KEY=sk-or-...-key" >> "$env_file"
  fi

  # Find the managed Node.
  local node_exe
  for cand in \
    "$HERMES_HOME/node/bin/node" \
    "$HERMES_HOME/bin/node/bin/node" \
    "$INSTALL_DIR/node/bin/node"; do
    [ -f "$cand" ] && { node_exe="$cand"; break; }
  done
  node_exe="${node_exe:-$(which node 2>/dev/null || echo "")}"
  [ -n "$node_exe" ] || fail "no node found"

  # Set up the Playwright driver.
  local pw_dir="$WORK_ROOT/pw-driver"
  mkdir -p "$pw_dir"
  local npm_cli
  npm_cli="$(dirname "$node_exe")/node_modules/npm/bin/npm-cli.js"
  [ -f "$npm_cli" ] || npm_cli="$(which npm 2>/dev/null)"
  (cd "$pw_dir" && "$node_exe" "$npm_cli" install --no-save --no-audit --no-fund "@playwright/test@1.55.0" 2>&1 | tail -5)

  cp "$SCRIPT_DIR/e2e-assets/drive-update.cjs" "$pw_dir/" 2>/dev/null || true

  local proof_dir="$WORK_ROOT/proof"
  mkdir -p "$proof_dir"
  (cd "$pw_dir" && "$node_exe" "$pw_dir/drive-update.cjs" "$app_bin" "$proof_dir" 2>&1 | tee -a "$LOG_DIR/update-drive.log")
}

# ── Linux: drive the update via Playwright ───────────────────────────
phase_update_linux() {
  local appimage="${DESKTOP_APPIMAGE:-}"
  [ -n "$appimage" ] || fail "no DESKTOP_APPIMAGE in state"
  [ -f "$appimage" ] || fail "AppImage not found: $appimage"

  # Seed a provider.
  local env_file="$HERMES_HOME/.env"
  if [ ! -f "$env_file" ] || ! grep -q "OPENROUTER_API_KEY" "$env_file" 2>/dev/null; then
    echo "OPENROUTER_API_KEY=sk-or-...-key" >> "$env_file"
  fi

  # Find the managed Node.
  local node_exe
  for cand in \
    "$HERMES_HOME/node/bin/node" \
    "$HERMES_HOME/bin/node/bin/node" \
    "$INSTALL_DIR/node/bin/node"; do
    [ -f "$cand" ] && { node_exe="$cand"; break; }
  done
  node_exe="${node_exe:-$(which node 2>/dev/null || echo "")}"
  [ -n "$node_exe" ] || fail "no node found"

  # Set up the Playwright driver.
  local pw_dir="$WORK_ROOT/pw-driver"
  mkdir -p "$pw_dir"
  local npm_cli
  npm_cli="$(dirname "$node_exe")/node_modules/npm/bin/npm-cli.js"
  [ -f "$npm_cli" ] || npm_cli="$(which npm 2>/dev/null)"
  (cd "$pw_dir" && "$node_exe" "$npm_cli" install --no-save --no-audit --no-fund "@playwright/test@1.55.0" 2>&1 | tail -5)

  cp "$SCRIPT_DIR/e2e-assets/drive-update.cjs" "$pw_dir/" 2>/dev/null || true

  local proof_dir="$WORK_ROOT/proof"
  mkdir -p "$proof_dir"
  # AppImages need --no-sandbox on CI runners.
  (cd "$pw_dir" && "$node_exe" "$pw_dir/drive-update.cjs" "$appimage" "$proof_dir" 2>&1 | tee -a "$LOG_DIR/update-drive.log")
}

# ── Dispatch ─────────────────────────────────────────────────────────
log "Bundled desktop install+update E2E driver"
log "  OS:       $OS"
log "  prev-tag: $PREV_TAG"
log "  head-sha: $HEAD_SHA"
log "  prev bundles: $PREV_BUNDLES"
log "  head bundles: $HEAD_BUNDLES"

phase_stage
phase_install
phase_update

log ""
log "Bundled E2E completed successfully."
