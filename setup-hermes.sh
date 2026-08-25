#!/bin/bash
# ============================================================================
# Hermes Agent Setup — dev-checkout wrapper
# ============================================================================
# Quick setup for developers who cloned the repo manually.
#
# This used to be a fourth, parallel implementation of "install Hermes":
# its own uv installer (astral-latest via curl|sh — unpinned, unverified),
# its own dependency tiers. It is now a WRAPPER
# over the same engine every other install path uses:
#
#   1. pinned uv into the machine-wide tool store  (generated fragment
#      below — same pin table, same digest check as install.sh)
#   2. venv + deps        via  python -m hermes_cli.venv_sync
#   3. managed runtimes   via  python -m installation.provisioner
#   4. user state         via  python -m hermes_cli.post_update
#
# The only logic that lives HERE is what is unique to a dev checkout:
# where to symlink the CLI.
#
# Usage:
#   ./setup-hermes.sh
# ============================================================================

set -e

GREEN='\033[0;32m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Prevent uv from discovering config files (uv.toml, pyproject.toml) from the
# wrong user's home directory when running under sudo -u <user>.  See #21269.
export UV_NO_CONFIG=1

get_command_link_dir() {
    echo "$HOME/.local/bin"
}

log_info()    { echo -e "${CYAN}→${NC} $*"; }
log_success() { echo -e "${GREEN}✓${NC} $*"; }
log_error()   { echo -e "${RED}✗${NC} $*"; }

echo ""
echo -e "${CYAN}⚕ Hermes Agent Setup${NC}"
echo ""

# --- BEGIN GENERATED: bootstrap pins (scripts/gen-bootstrap-pins.py) ---
# Derived from installation/runtime-pins.json. DO NOT EDIT BY HAND:
# run scripts/gen-bootstrap-pins.py after a pin bump.
UV_PIN_VERSION="0.12.3"
PYTHON_PIN_VERSION="3.11.15"

# Sets UV_PIN_URL + UV_PIN_SHA256 for a <os>-<arch> target key.
uv_bootstrap_pin() {
    case "$1" in
        linux-x64)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-x86_64-unknown-linux-gnu.tar.gz"
            UV_PIN_SHA256="600cf9a742aca00d292673b16b5acffaa7b8c269a364ad0c2e79498dcb1fe101"
            ;;
        linux-arm64)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-aarch64-unknown-linux-gnu.tar.gz"
            UV_PIN_SHA256="bb66cb52e7b1823aed1183630d8d8e5c958840d584a4c55ec10a4cfc168dcca2"
            ;;
        darwin-x64)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-x86_64-apple-darwin.tar.gz"
            UV_PIN_SHA256="4c9f52262a14da336e4a42ed24992d12d0c956acde87619e4611d321dffa602b"
            ;;
        darwin-arm64)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-aarch64-apple-darwin.tar.gz"
            UV_PIN_SHA256="546f7f8a6c70ff13a3a9d2bc958db3427298cebf3e0cb756f9177133b7068843"
            ;;
        *)
            UV_PIN_URL=""
            UV_PIN_SHA256=""
            return 1
            ;;
    esac
}
# --- END GENERATED: bootstrap pins ---

# Map this host to a pin-table target key (<os>-<arch>, Node spellings).
uv_bootstrap_target() {
    local _arch _os
    case "$(uname -m)" in
        arm64|aarch64) _arch="arm64" ;;
        x86_64|amd64)  _arch="x64" ;;
        *) return 1 ;;
    esac
    case "$(uname -s)" in
        Linux)  _os="linux" ;;
        Darwin) _os="darwin" ;;
        *) return 1 ;;
    esac
    echo "$_os-$_arch"
}

# ========================================================================
# Pinned uv into the tool store — the SAME artifact every installer uses.
# No astral-latest, no curl|sh: URL + sha256 come from the generated
# fragment above, which derives from installation/runtime-pins.json.
# ========================================================================
_target="$(uv_bootstrap_target)" || { log_error "Unsupported platform"; exit 1; }
uv_bootstrap_pin "$_target" || { log_error "No uv pin for $_target"; exit 1; }

_home_root="${HERMES_HOME:-$HOME/.hermes}"
case "$_home_root" in
    */profiles/*) _home_root="${_home_root%/profiles/*}" ;;
esac
_store="$_home_root/tools"
_entry="$_store/uv-$UV_PIN_VERSION-$_target"
UV_CMD="$_entry/uv"

if [ ! -x "$UV_CMD" ]; then
    log_info "Staging pinned uv $UV_PIN_VERSION into the tool store..."
    _tmp="$(mktemp -d)"
    curl -LsSf "$UV_PIN_URL" -o "$_tmp/uv.tar.gz"
    if command -v sha256sum >/dev/null 2>&1; then
        _digest="$(sha256sum "$_tmp/uv.tar.gz" | cut -d' ' -f1)"
    else
        _digest="$(shasum -a 256 "$_tmp/uv.tar.gz" | cut -d' ' -f1)"
    fi
    if [ "$_digest" != "$UV_PIN_SHA256" ]; then
        log_error "uv digest mismatch (expected $UV_PIN_SHA256, got $_digest)"
        rm -rf "$_tmp"; exit 1
    fi
    mkdir -p "$_store"
    _staging="$_store/.staging-$$-$(date +%s)"
    mkdir -p "$_staging"
    tar -xzf "$_tmp/uv.tar.gz" -C "$_tmp"
    _unpacked="$(find "$_tmp" -mindepth 1 -maxdepth 2 -name uv -type f | head -n1)"
    [ -n "$_unpacked" ] || { log_error "uv missing from archive"; rm -rf "$_tmp" "$_staging"; exit 1; }
    mv "$_unpacked" "$_staging/uv"
    [ -f "$(dirname "$_unpacked")/uvx" ] && mv "$(dirname "$_unpacked")/uvx" "$_staging/uvx"
    chmod +x "$_staging/uv" "$_staging/uvx" 2>/dev/null || true
    cat > "$_staging/.hermes-store-entry.json" <<MARKER
{"tool": "uv", "version": "$UV_PIN_VERSION", "target": "$_target", "sha256": "$UV_PIN_SHA256", "publishedAt": "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"}
MARKER
    rm -rf "$_tmp"
    if ! mv "$_staging" "$_entry" 2>/dev/null; then
        rm -rf "$_staging"
        [ -x "$UV_CMD" ] || { log_error "uv publish race lost and no winner found"; exit 1; }
    fi
fi
log_success "Pinned uv ready ($("$UV_CMD" --version 2>/dev/null))"

# ========================================================================
# venv + deps: hand the rest to the shared engine.
# ========================================================================
if [ ! -d venv ]; then
    log_info "Creating venv..."
    "$UV_CMD" venv venv --python "$PYTHON_PIN_VERSION" >/dev/null
    log_success "venv created"
fi

log_info "Syncing dependencies (venv_sync — hash-verified via uv.lock)..."
if ! venv/bin/python -m hermes_cli.venv_sync 2>/dev/null; then
    # A fresh venv has no hermes_cli yet; seed it with one uv sync,
    # which is exactly what venv_sync would have run.
    UV_PROJECT_ENVIRONMENT="$SCRIPT_DIR/venv" "$UV_CMD" sync --extra all --locked
fi
log_success "Dependencies installed"

log_info "Provisioning managed runtimes (node, npm, git, gh, ripgrep)..."
"$UV_CMD" run --no-project python -m installation.provisioner || {
    log_error "Runtime provisioning failed — re-run after checking your network"
    exit 1
}

# ============================================================================
# .env seed + CLI symlink (the only genuinely dev-checkout-specific parts)
# ============================================================================
if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    log_success ".env created from template"
fi

LINK_DIR="$(get_command_link_dir)"
mkdir -p "$LINK_DIR"
ln -sf "$SCRIPT_DIR/venv/bin/hermes" "$LINK_DIR/hermes"
log_success "hermes CLI linked into $LINK_DIR"

# User-state steps (config migration, skills seed) via the shared engine.
venv/bin/python -m hermes_cli.post_update --scope all || true

echo ""
log_success "Setup complete. Run: hermes"
