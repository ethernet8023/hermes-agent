#!/bin/bash
# ============================================================================
# Hermes Agent Installer
# ============================================================================
# Installation script for Linux and macOS.
# Uses uv to provision Python and install dependencies.
#
# Usage:
#   curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
#
# Or with options:
#   curl -fsSL ... | bash -s -- --no-venv --skip-setup
#
# ============================================================================

set -e

# Guard against environment leakage when the installer is launched from another
# Python-driven tool session (e.g. Hermes terminal tool). A pre-set PYTHONPATH
# can force pip/entrypoints to import a different checkout than the one being
# installed, which makes fresh installs appear broken or stale.
if [ -n "${PYTHONPATH:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONPATH during install to avoid module shadowing"
    unset PYTHONPATH
fi
if [ -n "${PYTHONHOME:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONHOME during install"
    unset PYTHONHOME
fi

# Prevent uv from discovering config files (uv.toml, pyproject.toml) from the
# wrong user's home directory when running under sudo -u <user>.  See #21269.
export UV_NO_CONFIG=1

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# Configuration
REPO_URL_SSH="git@github.com:NousResearch/hermes-agent.git"
REPO_URL_HTTPS="https://github.com/NousResearch/hermes-agent.git"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# INSTALL_DIR is resolved AFTER arg parsing and OS detection so we can pick an
# FHS-style layout for root installs.  Track whether the user gave us an
# explicit directory — if so we never override it.
if [ -n "${HERMES_INSTALL_DIR:-}" ]; then
    INSTALL_DIR="$HERMES_INSTALL_DIR"
    INSTALL_DIR_EXPLICIT=true
else
    INSTALL_DIR=""
    INSTALL_DIR_EXPLICIT=false
fi
PYTHON_VERSION="3.11"

# FHS-style root install layout (set by resolve_install_layout when applicable):
#   code at /usr/local/lib/hermes-agent, command at /usr/local/bin/hermes,
#   data still at /root/.hermes (HERMES_HOME).  Matches Claude Code / Codex CLI
#   and keeps Docker bind-mounted /root/ volumes lean.
ROOT_FHS_LAYOUT=false
DETECTED_BROWSER_EXECUTABLE=""

# Options
USE_VENV=true
RUN_SETUP=true
SKIP_BROWSER=false
SKIP_COMPUTER_USE=false
NO_SKILLS=false
BRANCH="main"
INSTALL_COMMIT=""
FORCE_COMMIT=false

MANIFEST_MODE=false
STAGE_NAME=""
JSON_OUTPUT=false
NON_INTERACTIVE=false
INCLUDE_DESKTOP=false

# Detect non-interactive mode (e.g. curl | bash)
# When stdin is not a terminal, read -p will fail with EOF,
# causing set -e to silently abort the entire script.
if [ -t 0 ]; then
    IS_INTERACTIVE=true
else
    IS_INTERACTIVE=false
fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --no-venv)
            USE_VENV=false
            shift
            ;;
        --skip-setup)
            RUN_SETUP=false
            shift
            ;;
        --skip-browser|--no-playwright)
            SKIP_BROWSER=true
            shift
            ;;
        --skip-computer-use)
            SKIP_COMPUTER_USE=true
            shift
            ;;
        --no-skills)
            NO_SKILLS=true
            shift
            ;;
        --branch|-Branch)
            BRANCH="$2"
            shift 2
            ;;
        --commit|-Commit)
            INSTALL_COMMIT="$2"
            shift 2
            ;;
        --force-commit|-ForceCommit)
            FORCE_COMMIT=true
            shift
            ;;
        --manifest|-Manifest)
            MANIFEST_MODE=true
            shift
            ;;
        --stage|-Stage)
            STAGE_NAME="$2"
            shift 2
            ;;
        --json|-Json)
            JSON_OUTPUT=true
            shift
            ;;
        --non-interactive|-NonInteractive)
            NON_INTERACTIVE=true
            shift
            ;;
        --include-desktop|-IncludeDesktop)
            INCLUDE_DESKTOP=true
            shift
            ;;
        --dir)
            INSTALL_DIR="$2"
            INSTALL_DIR_EXPLICIT=true
            shift 2
            ;;
        --hermes-home)
            HERMES_HOME="$2"
            shift 2
            ;;

        -h|--help)
            echo "Hermes Agent Installer"
            echo ""
            echo "Usage: install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --no-venv      Don't create virtual environment"
            echo "  --skip-setup   Skip interactive setup wizard"
            echo "  --skip-browser Skip staging the pinned browser (browser tools won't work)"
            echo "  --skip-computer-use  Skip the cua-driver (Computer Use) install"
            echo "  --no-skills    Start with a blank slate — seed no bundled skills, and"
            echo "                   write \$HERMES_HOME/.no-bundled-skills so future"
            echo "                   'hermes update' runs never inject bundled skills either"
            echo "  --branch NAME  Git branch to install (default: main)"
            echo "  --commit SHA   Pin checkout to a specific commit after clone/update"
            echo "                   (ignored when it would roll an existing install back)"
            echo "  --force-commit Apply --commit even if it rolls the install backwards"
            echo "  --manifest     Print desktop bootstrap stage manifest as JSON"
            echo "  --stage NAME   Run one desktop bootstrap stage"
            echo "  --json         Print a JSON result frame for --stage"
            echo "  --non-interactive  Skip stages that require user input"
            echo "  --include-desktop  Also build the desktop app (apps/desktop -> Hermes.app)"
            echo "  --dir PATH     Installation directory"
            echo "                   default (non-root):  ~/.hermes/hermes-agent"
            echo "                   default (root, Linux): /usr/local/lib/hermes-agent"
            echo "  --hermes-home PATH  Data directory (default: ~/.hermes, or \$HERMES_HOME)"
            echo "  -h, --help     Show this help"
            echo ""
            echo "Notes:"
            echo "  When running as root on Linux, Hermes installs the code under"
            echo "  /usr/local/lib/hermes-agent and links the command into"
            echo "  /usr/local/bin/hermes (FHS layout — matches Claude Code / Codex CLI)."
            echo "  Data, config, sessions, and logs still live in \$HERMES_HOME"
            echo "  (default /root/.hermes).  This keeps Docker bind-mounted volumes"
            echo "  small and ensures the command is on PATH for all shells."
            echo "  Existing installs at \$HERMES_HOME/hermes-agent are preserved in-place."

            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# ============================================================================
# Helper functions
# ============================================================================

print_banner() {
    echo ""
    echo -e "${MAGENTA}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│             ⚕ Hermes Agent Installer                    │"
    echo "├─────────────────────────────────────────────────────────┤"
    echo "│  An open source AI agent by Nous Research.              │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
}

log_info() {
    echo -e "${CYAN}→${NC} $1"
}

log_success() {
    echo -e "${GREEN}✓${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

log_error() {
    echo -e "${RED}✗${NC} $1"
}

json_escape() {
    # Enough for short installer status strings; avoids requiring jq during
    # pre-install bootstrap.
    printf '%s' "$1" | tr '\n' ' ' | sed \
        -e 's/\\/\\\\/g' \
        -e 's/"/\\"/g'
}

# npm rewrites tracked package-lock.json files non-deterministically during
# `npm install` / `npm run pack`. On a managed install those diffs are never
# intentional, but they leave the checkout dirty — which forces `hermes update`
# to autostash on every run and makes branch switches fragile. Restore them so
# a fresh install ends with a clean tree. Best-effort; only touches lockfiles.
restore_dirty_lockfiles() {
    local repo="${1:-$INSTALL_DIR}"
    [ -n "$repo" ] && [ -d "$repo/.git" ] || return 0
    command -v git >/dev/null 2>&1 || return 0
    local dirty
    dirty=$(git -C "$repo" diff --name-only 2>/dev/null | grep 'package-lock\.json$' || true)
    [ -z "$dirty" ] && return 0
    echo "$dirty" | while IFS= read -r f; do
        [ -n "$f" ] && git -C "$repo" checkout -- "$f" 2>/dev/null || true
    done
}

# npm rewrites tracked package-lock.json files non-deterministically during
# local builds. On a managed install those diffs are usually runtime churn, not
# intentional user edits, so discard them before the repository-stage stash.
# If package.json in the same directory is also dirty we keep both changes.
discard_update_lockfile_churn() {
    local repo="${1:-$INSTALL_DIR}"
    [ -n "$repo" ] && [ -d "$repo/.git" ] || return 0
    command -v git >/dev/null 2>&1 || return 0

    local dirty_diff
    dirty_diff=$(git -C "$repo" diff --name-only 2>/dev/null) || return 0
    [ -n "$dirty_diff" ] || return 0

    local dirty_package_dirs=""
    while IFS= read -r path; do
        case "$path" in
            *package.json)
                dirty_package_dirs="${dirty_package_dirs}$(dirname "$path")"$'\n'
                ;;
        esac
    done <<EOF
$dirty_diff
EOF

    local dirty_locks=""
    local dirty_count=0
    while IFS= read -r path; do
        case "$path" in
            *package-lock.json)
                local lock_dir
                lock_dir=$(dirname "$path")
                case $'\n'"$dirty_package_dirs" in
                    *$'\n'"$lock_dir"$'\n'*) continue ;;
                esac
                dirty_locks="${dirty_locks}${path}"$'\n'
                dirty_count=$((dirty_count + 1))
                ;;
        esac
    done <<EOF
$dirty_diff
EOF

    [ "$dirty_count" -gt 0 ] || return 0
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        git -C "$repo" checkout -- "$path" 2>/dev/null || true
    done <<EOF
$dirty_locks
EOF
    log_info "Discarded npm lockfile churn (${dirty_count} file(s))"
}

emit_manifest() {
    # Stage-Desktop is included only with --include-desktop, mirroring
    # install.ps1: the signed bootstrap installer (Hermes-Setup) passes it so
    # a GUI install ends up with a launchable app; the Electron app's own
    # first-launch bootstrap and the CLI one-liner omit it (building the
    # desktop from inside the already-running app would clobber it).
    local desktop_stage=""
    if [ "$INCLUDE_DESKTOP" = true ]; then
        desktop_stage='{"name":"desktop","title":"Build desktop app","category":"runtime","needs_user_input":false},'
    fi
    printf '%s' '{"protocol_version":1,"stages":[{"name":"prerequisites","title":"System prerequisites","category":"runtime","needs_user_input":false},{"name":"repository","title":"Download Hermes Agent","category":"runtime","needs_user_input":false},{"name":"venv","title":"Create Python virtual environment","category":"runtime","needs_user_input":false},{"name":"python-deps","title":"Install Python dependencies","category":"runtime","needs_user_input":false},{"name":"node-deps","title":"Install browser-tool dependencies","category":"runtime","needs_user_input":false},{"name":"path","title":"Install hermes command","category":"runtime","needs_user_input":false},{"name":"config","title":"Prepare config and skills","category":"configuration","needs_user_input":false},{"name":"setup","title":"Configure API keys and settings","category":"configuration","needs_user_input":true},{"name":"gateway","title":"Configure gateway service","category":"configuration","needs_user_input":true},'"$desktop_stage"'{"name":"complete","title":"Finish install","category":"runtime","needs_user_input":false}]}'
    printf '\n'
}

stage_needs_user_input() {
    case "$1" in
        setup|gateway) return 0 ;;
        *) return 1 ;;
    esac
}

emit_stage_json() {
    local stage="$1"
    local ok="$2"
    local skipped="${3:-false}"
    local reason="${4:-}"
    local escaped_reason
    escaped_reason="$(json_escape "$reason")"
    if [ -n "$escaped_reason" ]; then
        printf '{"ok":%s,"stage":"%s","skipped":%s,"reason":"%s"}\n' "$ok" "$stage" "$skipped" "$escaped_reason"
    else
        printf '{"ok":%s,"stage":"%s","skipped":%s}\n' "$ok" "$stage" "$skipped"
    fi
}

prompt_yes_no() {
    local question="$1"
    local default="${2:-yes}"
    local prompt_suffix
    local answer=""

    # Use case patterns (not ${var,,}) so this works on bash 3.2 (macOS /bin/bash).
    case "$default" in
        [yY]|[yY][eE][sS]|[tT][rR][uU][eE]|1) prompt_suffix="[Y/n]" ;;
        *) prompt_suffix="[y/N]" ;;
    esac

    if [ "$NON_INTERACTIVE" = true ]; then
        answer=""
    elif [ "$IS_INTERACTIVE" = true ]; then
        read -r -p "$question $prompt_suffix " answer || answer=""
    elif [ -r /dev/tty ] && [ -w /dev/tty ]; then
        printf "%s %s " "$question" "$prompt_suffix" > /dev/tty
        IFS= read -r answer < /dev/tty || answer=""
    else
        answer=""
    fi

    answer="${answer#"${answer%%[![:space:]]*}"}"
    answer="${answer%"${answer##*[![:space:]]}"}"

    if [ -z "$answer" ]; then
        case "$default" in
            [yY]|[yY][eE][sS]|[tT][rR][uU][eE]|1) return 0 ;;
            *) return 1 ;;
        esac
    fi

    case "$answer" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *) return 1 ;;
    esac
}

# Decide where the repo checkout + venv live, and where the `hermes` command
# symlink goes.  Called after detect_os so $OS/$DISTRO are known.
#
# Defaults:
#   - Non-root, any OS:       INSTALL_DIR = $HERMES_HOME/hermes-agent
#                             command link in $HOME/.local/bin
#   - Root on Linux (new):    INSTALL_DIR = /usr/local/lib/hermes-agent
#                             command link in /usr/local/bin
#                             (unless a legacy install already exists at
#                              $HERMES_HOME/hermes-agent — then preserve it)
#
# Always no-op when the user set --dir or $HERMES_INSTALL_DIR.
resolve_install_layout() {
    if [ "$INSTALL_DIR_EXPLICIT" = true ]; then
        log_info "Install directory: $INSTALL_DIR (explicit)"
        return 0
    fi

    # Root on Linux: prefer FHS layout unless a legacy install already exists.
    # macOS root installs keep the legacy layout because /usr/local/ on macOS
    # is Homebrew territory and we don't want to fight that.
    if [ "$OS" = "linux" ] && [ "$(id -u)" -eq 0 ]; then
        if [ -d "$HERMES_HOME/hermes-agent/.git" ]; then
            INSTALL_DIR="$HERMES_HOME/hermes-agent"
            log_info "Existing install detected at $INSTALL_DIR — keeping legacy layout"
            log_info "  (new root installs use /usr/local/lib/hermes-agent)"
            return 0
        fi
        INSTALL_DIR="/usr/local/lib/hermes-agent"
        ROOT_FHS_LAYOUT=true
        # Place uv-managed Python under /usr/local/share so the venv interpreter
        # is world-readable.  Default uv paths land in /root/.local/share/uv,
        # which non-root users can't traverse — leaving the shared
        # /usr/local/bin/hermes wrapper unable to exec the bad-interpreter venv
        # python.  See #21457.
        export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/usr/local/share/uv/python}"
        export UV_PYTHON_BIN_DIR="${UV_PYTHON_BIN_DIR:-/usr/local/share/uv/bin}"
        log_info "Root install on Linux — using FHS layout"
        log_info "  Code:    $INSTALL_DIR"
        log_info "  Command: /usr/local/bin/hermes"
        log_info "  Data:    $HERMES_HOME (unchanged)"
        log_info "  uv Python: $UV_PYTHON_INSTALL_DIR (world-readable)"
        return 0
    fi

    # Default: non-root → legacy user-scoped layout.
    INSTALL_DIR="$HERMES_HOME/hermes-agent"
}

get_command_link_dir() {
    if [ "$ROOT_FHS_LAYOUT" = true ]; then
        echo "/usr/local/bin"
    else
        echo "$HOME/.local/bin"
    fi
}

get_command_link_display_dir() {
    if [ "$ROOT_FHS_LAYOUT" = true ]; then
        echo '/usr/local/bin'
    else
        echo '~/.local/bin'
    fi
}

get_hermes_command_path() {
    local link_dir
    link_dir="$(get_command_link_dir)"
    if [ -x "$link_dir/hermes" ]; then
        echo "$link_dir/hermes"
    else
        echo "hermes"
    fi
}

# ============================================================================
# System detection
# ============================================================================

detect_os() {
    case "$(uname -s)" in
        Linux*)
            OS="linux"
            if [ -f /etc/os-release ]; then
                . /etc/os-release
                DISTRO="$ID"
                # VERSION_ID (e.g. "26.04", "14") lets us tell whether the
                # apt release is newer than the newest one Playwright's
                # platform resolver recognizes — the #35166 hang condition.
                DISTRO_VERSION="${VERSION_ID:-}"
            else
                DISTRO="unknown"
                DISTRO_VERSION=""
            fi
            ;;
        Darwin*)
            OS="macos"
            DISTRO="macos"
            ;;
        CYGWIN*|MINGW*|MSYS*)
            OS="windows"
            DISTRO="windows"
            log_error "Windows detected. Please use the PowerShell installer:"
            log_info "  iex (irm https://hermes-agent.nousresearch.com/install.ps1)"
            exit 1
            ;;
        *)
            OS="unknown"
            DISTRO="unknown"
            log_warn "Unknown operating system"
            ;;
    esac

    log_success "Detected: $OS ($DISTRO)"
}

# ============================================================================
# Dependency checks
# ============================================================================

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
    local _arch
    case "$(uname -m)" in
        arm64|aarch64) _arch="arm64" ;;
        x86_64|amd64)  _arch="x64" ;;
        *) return 1 ;;
    esac
    case "$OS" in
        linux)  echo "linux-$_arch" ;;
        macos)  echo "darwin-$_arch" ;;
        *) return 1 ;;
    esac
}

install_uv() {
    # Hermes owns its own uv at $INSTALL_DIR/.hermes-runtime/uv/uv — the
    # SAME install-scoped location managed_uv.managed_uv_path() resolves,
    # so the binary this installer stages is the one every later
    # `hermes update` / ensure_uv() finds. No PATH probing, no conda
    # guards, no second copy in $HERMES_HOME/bin.
    # The binary is the EXACT artifact pinned in installation/runtime-pins.json
    # (URL + sha256 via the generated fragment above): the same authority the
    # provisioner uses for every other managed tool. No astral-latest.
    # Runs AFTER clone_repo: the install dir must exist to hold it.
    require_install_dir
    local _uv_dir="$INSTALL_DIR/.hermes-runtime/uv"
    local _managed_uv="$_uv_dir/uv"

    if [ -x "$_managed_uv" ]; then
        UV_VERSION=$($_managed_uv --version 2>/dev/null)
        # Keep it only when it IS the pinned version — a pin bump must
        # propagate, and a stale binary predating the pin is unverified.
        case "$UV_VERSION" in
            *" $UV_PIN_VERSION"*|"uv $UV_PIN_VERSION")
                UV_CMD="$_managed_uv"
                log_success "Managed uv found ($UV_VERSION)"
                return 0
                ;;
            *)
                log_info "Managed uv is ${UV_VERSION:-unreadable}, pin is $UV_PIN_VERSION — replacing"
                ;;
        esac
    fi

    local _target
    if ! _target="$(uv_bootstrap_target)"; then
        log_error "No pinned uv build for this platform ($(uname -s) $(uname -m))"
        log_info "Install manually: https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi
    uv_bootstrap_pin "$_target"

    log_info "Installing pinned uv $UV_PIN_VERSION into $_uv_dir ..."
    mkdir -p "$_uv_dir"

    local _uv_tmp
    _uv_tmp="$(mktemp -d 2>/dev/null || echo "/tmp/hermes-uv-bootstrap.$$")"
    mkdir -p "$_uv_tmp"

    if ! curl -LsSf "$UV_PIN_URL" -o "$_uv_tmp/uv.tar.gz"; then
        log_error "Failed to download pinned uv from $UV_PIN_URL"
        log_info "Install manually: https://docs.astral.sh/uv/getting-started/installation/"
        rm -rf "$_uv_tmp"
        exit 1
    fi

    # Digest check BEFORE extraction — a mismatched archive is deleted,
    # never unpacked. sha256sum on Linux, shasum -a 256 on macOS.
    local _digest
    if command -v sha256sum >/dev/null 2>&1; then
        _digest="$(sha256sum "$_uv_tmp/uv.tar.gz" | cut -d' ' -f1)"
    else
        _digest="$(shasum -a 256 "$_uv_tmp/uv.tar.gz" | cut -d' ' -f1)"
    fi
    if [ "$_digest" != "$UV_PIN_SHA256" ]; then
        log_error "uv download digest mismatch (expected $UV_PIN_SHA256, got $_digest)"
        log_info "The download may be corrupted or tampered with. Re-run the installer."
        rm -rf "$_uv_tmp"
        exit 1
    fi

    # The tarball nests everything under one versioned dir (uv-<triple>/).
    # Move uv AND uvx — browser tooling resolves uvx from the same dir.
    if ! tar -xzf "$_uv_tmp/uv.tar.gz" -C "$_uv_tmp"; then
        log_error "Failed to extract uv archive"
        rm -rf "$_uv_tmp"
        exit 1
    fi
    local _unpacked
    _unpacked="$(find "$_uv_tmp" -mindepth 1 -maxdepth 2 -name uv -type f | head -n1)"
    if [ -z "$_unpacked" ]; then
        log_error "uv binary not found inside the downloaded archive"
        rm -rf "$_uv_tmp"
        exit 1
    fi
    mv "$_unpacked" "$_managed_uv"
    if [ -f "$(dirname "$_unpacked")/uvx" ]; then
        mv "$(dirname "$_unpacked")/uvx" "$_uv_dir/uvx"
        chmod +x "$_uv_dir/uvx"
    fi
    chmod +x "$_managed_uv"
    rm -rf "$_uv_tmp"

    UV_CMD="$_managed_uv"
    UV_VERSION=$($UV_CMD --version 2>/dev/null)
    if [ -z "$UV_VERSION" ]; then
        log_error "Pinned uv staged but does not run on this host"
        exit 1
    fi
    log_success "Managed uv installed ($UV_VERSION)"
}

check_python() {
    log_info "Checking Python $PYTHON_VERSION..."

    # Decision 3: the exact interpreter comes from the generated pin
    # fragment, unconditionally — the fragment ships in the same commit
    # as this script, and gen-bootstrap-pins.py refuses to generate one
    # without the pin. PYTHON_VERSION (the family) survives only for the
    # log line above.
    local _py_pin="$PYTHON_PIN_VERSION"

    # Let uv handle Python — it can download and manage Python versions
    # First check if the PINNED interpreter is already available
    if PYTHON_PATH="$("$UV_CMD" python find "$_py_pin" 2>/dev/null)"; then
        PYTHON_FOUND_VERSION="$("$PYTHON_PATH" --version 2>/dev/null)"
        log_success "Python found: $PYTHON_FOUND_VERSION"
        return 0
    fi

    # Python not found — use uv to install it (no sudo needed!)
    log_info "Python $_py_pin not found, installing via uv..."
    if "$UV_CMD" python install "$_py_pin"; then
        PYTHON_PATH="$("$UV_CMD" python find "$_py_pin")"
        PYTHON_FOUND_VERSION="$("$PYTHON_PATH" --version 2>/dev/null)"
        log_success "Python installed: $PYTHON_FOUND_VERSION"
    else
        log_error "Failed to install Python $_py_pin"
        log_info "Install Python $_py_pin manually, then re-run this script"
        exit 1
    fi
}

# git is required to clone the repo. macOS and Linux use the machine's git
# by design: a 147MB dugite download to run `git clone` is the wrong trade
# where git is one package-manager command away, so this reports the fix
# instead of bundling one. Windows is different and install.ps1 owns it:
# git bash ships inside PortableGit there.
check_git() {
    log_info "Checking Git..."

    # A working system git is enough for the clone — decision 1
    # (system-git-first): the provisioner later records it as a fact and
    # the pinned dugite git only exists for boxes with none. On fresh
    # macOS /usr/bin/git is the xcode-select shim: a stub that exits
    # non-zero AND pops a system dialog if invoked carelessly, so the
    # `git --version` probe below must be the only thing we run.
    if command -v git &> /dev/null && git --version &> /dev/null; then
        GIT_VERSION=$(git --version | awk '{print $3}')
        log_success "Git $GIT_VERSION found"
        return 0
    fi

    log_error "Git not found. Install it and re-run:"
    case "$OS" in
        linux)
            log_info "  Debian/Ubuntu: sudo apt-get install git"
            log_info "  Fedora:        sudo dnf install git"
            log_info "  Arch:          sudo pacman -S git"
            ;;
        macos)
            log_info "  brew install git    (or xcode-select --install)"
            ;;
    esac
    exit 1
}

provision_managed_runtimes() {
    # THE dep engine, shared with `hermes update`: one implementation of
    # "download the pinned tools", in Python, reading runtime-pins.json.
    #
    # Run with `uv run --no-project`, NOT the venv python. The installation
    # package is stdlib-only by contract (tests/test_installation_stdlib_only.py
    # runs it under an interpreter with no site-packages), so provisioning
    # does not need the venv and must not wait for it: uv is the one thing
    # bootstrapped before this point, and the tools installed here are what
    # later stages build with.
    #
    # FATAL on failure. Hermes runs its own toolchain -- the versions in
    # runtime-pins.json are the ones every `npm ci` and every managed
    # subprocess uses, and `engine-strict=true` means a system Node that
    # happens to be present is not a substitute. An install that continues
    # without them produces a Hermes that cannot build the web UI, cannot
    # run browser tools, and reports no error until the user hits one of
    # those. Better to fail here, where the cause is on screen.
    log_info "Provisioning managed runtimes (node, npm, uv, git, gh, ripgrep)..."

    if [ ! -d "$INSTALL_DIR/installation" ]; then
        log_error "No installation package at $INSTALL_DIR/installation"
        log_error "The repository download did not complete."
        return 1
    fi
    # Stages run in separate processes, so $UV_CMD from an earlier stage is
    # gone. install_uv reuses the managed binary when it is already there,
    # so this is a no-op in the common case.
    if [ -z "$UV_CMD" ]; then
        install_uv
    fi
    if [ -z "$UV_CMD" ]; then
        log_error "uv is not available -- cannot run the provisioner"
        return 1
    fi

    # Downloads fail transiently: a dropped connection, a slow mirror, a
    # registry hiccup. Retrying costs seconds; failing the install costs the
    # user a reinstall. The provisioner is idempotent and skips tools that
    # are already at their pinned version, so a retry only re-fetches what
    # is still missing.
    #
    # The browser rides the same sweep: --extra agent-browser stages the
    # driver AND (through the pin table's requires edge) the pinned
    # Chromium pair, digest-verified into the shared store — replacing the
    # old `npx playwright install` download into ~/.cache/ms-playwright.
    # System LIBRARIES for that Chromium stay in the script (see the
    # browser-deps lane): root-privileged OS package work is not the
    # provisioner's business.
    #
    # cua-driver rides it too. It used to arrive through its own
    # `curl | bash` stage (install_computer_use_driver), which fetched
    # whatever release upstream had tagged, unverified by us, and left an
    # installer home + lock + symlink farm behind. As a pinned optional
    # tool it is one more --extra: exact version, digest checked before
    # extraction, and `hermes update` carries a pin bump from then on.
    local -a provisioner_args=()
    if [ "$SKIP_BROWSER" != true ]; then
        provisioner_args+=(--extra agent-browser)
    fi
    if [ "$SKIP_COMPUTER_USE" != true ]; then
        provisioner_args+=(--extra cua-driver)
    fi
    local attempt
    for attempt in 1 2 3; do
        if (cd "$INSTALL_DIR" && "$UV_CMD" run --no-project python -m installation.provisioner "${provisioner_args[@]}"); then
            log_success "Managed runtimes provisioned"
            # PATH for the REST OF THIS INSTALLER RUN (npm for browser deps,
            # node for ui-tui build). The provisioner publishes tool BYTES
            # into the machine-wide store (~/.hermes/tools/<tool>-<ver>-<target>/)
            # and records install-scoped FACTS in .hermes-runtime/runtimes.json;
            # ask the same reader every runtime consumer uses
            # (installation.env.managed_path_dirs) instead of hardcoding a
            # directory shape — the old literal
            # "$INSTALL_DIR/.hermes-runtime/node/bin" does not exist under the
            # store split, so npm was never found and the node-deps stage died
            # with "npm: command not found".
            local managed_dirs
            managed_dirs=$(cd "$INSTALL_DIR" && "$UV_CMD" run --no-project python -c '
import os
from installation import env
print(os.pathsep.join(str(d) for d in env.managed_path_dirs()))
' 2>/dev/null) || managed_dirs=""
            if [ -n "$managed_dirs" ]; then
                export PATH="$managed_dirs:$PATH"
            else
                log_warn "Could not resolve managed tool PATH from runtimes.json"
            fi
            return 0
        fi
        if [ "$attempt" -lt 3 ]; then
            log_warn "Provisioning attempt $attempt failed -- retrying..."
            sleep $((attempt * 2))
        fi
    done

    log_error "Could not provision the managed runtimes after 3 attempts."
    log_error "Hermes needs its own pinned Node, npm, git, gh and ripgrep;"
    log_error "a system copy is not a substitute (engine-strict=true)."
    log_info "Check your network and re-run the installer."
    return 1
}

check_network_prerequisites() {
    log_info "Checking internet connectivity for package install and web tools..."

    local url
    local failed=false
    local checks=("https://pypi.org/simple/" "https://duckduckgo.com/")

    if ! command -v curl >/dev/null 2>&1; then
        log_warn "curl not found; skipping connectivity probes"
        return 0
    fi

    # Run the probes in parallel — serially, two blocked probes cost
    # 2 × --max-time (16 s) before the user sees any useful error; in
    # parallel the worst case is one --max-time (8 s).
    local pids=()
    local tmpdir
    tmpdir=$(mktemp -d)
    local i=0
    for url in "${checks[@]}"; do
        (
            if curl -fsSI --max-time 8 "$url" >/dev/null 2>&1; then
                : > "$tmpdir/ok_$i"
            fi
        ) &
        pids+=($!)
        i=$((i + 1))
    done
    wait "${pids[@]}" 2>/dev/null

    i=0
    for url in "${checks[@]}"; do
        if [ ! -e "$tmpdir/ok_$i" ]; then
            failed=true
            log_warn "Could not reach $url"
        fi
        i=$((i + 1))
    done
    rm -rf "$tmpdir"

    if [ "$failed" = false ]; then
        log_success "Internet connectivity looks good"
        return 0
    fi

    log_warn "Network checks failed. Hermes install may complete, but web search and dependency downloads can fail."
    log_info "Verify internet/DNS and retry if pip install fails."
}

install_system_packages() {
    # Detect what's missing
    HAS_FFMPEG=false
    local need_ffmpeg=false
    local need_libatomic=false

    # Node >= 24 on arm64 Linux links against libatomic.so.1, which minimal
    # cloud/container images (Ubuntu 24.04 minimal, Debian slim) do not ship.
    # Without it the provisioner's verify-by-running step fails with
    # "error while loading shared libraries: libatomic.so.1" AFTER a correct
    # download+sha256, which reads like corruption. Only Linux needs the
    # probe; macOS links atomics from libSystem.
    if [ "$OS" = "linux" ]; then
        if ! ldconfig -p 2>/dev/null | grep -q "libatomic.so.1"; then
            need_libatomic=true
        fi
    fi

    log_info "Checking ffmpeg (TTS voice messages)..."
    if command -v ffmpeg &> /dev/null; then
        local ffmpeg_ver=$(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')
        log_success "ffmpeg $ffmpeg_ver found"
        HAS_FFMPEG=true
    else
        need_ffmpeg=true
    fi

    # Nothing to install — done
    if [ "$need_ffmpeg" = false ] && [ "$need_libatomic" = false ]; then
        return 0
    fi

    # Build a human-readable description + package list
    local desc_parts=()
    local pkgs=()
    if [ "$need_ffmpeg" = true ]; then
        desc_parts+=("ffmpeg for TTS voice messages")
        pkgs+=("ffmpeg")
    fi
    if [ "$need_libatomic" = true ]; then
        desc_parts+=("libatomic1 for the pinned Node runtime")
        case "$DISTRO" in
            fedora) pkgs+=("libatomic") ;;
            arch)   ;;  # part of gcc-libs, always present
            *)      pkgs+=("libatomic1") ;;
        esac
    fi
    local description
    description=$(IFS=" and "; echo "${desc_parts[*]}")
    # Everything needed is already covered (e.g. arch: libatomic ships in
    # gcc-libs) — nothing to install after all.
    if [ ${#pkgs[@]} -eq 0 ]; then
        return 0
    fi

    # ── macOS: brew ──
    if [ "$OS" = "macos" ]; then
        if command -v brew &> /dev/null; then
            log_info "Installing ${pkgs[*]} via Homebrew..."
            if brew install "${pkgs[@]}"; then
                [ "$need_ffmpeg" = true ]  && HAS_FFMPEG=true  && log_success "ffmpeg installed"
                return 0
            fi
        fi
        log_warn "Could not auto-install (brew not found or install failed)"
        log_info "Install manually: brew install ${pkgs[*]}"
        return 0
    fi

    # ── Linux: resolve package manager command ──
    local pkg_install=""
    case "$DISTRO" in
        ubuntu|debian) pkg_install="apt install -y"   ;;
        fedora)        pkg_install="dnf install -y"   ;;
        arch)          pkg_install="pacman -S --noconfirm" ;;
    esac

    if [ -n "$pkg_install" ]; then
        local install_cmd="$pkg_install ${pkgs[*]}"

        # Prevent needrestart/whiptail dialogs from blocking non-interactive installs
        case "$DISTRO" in
            ubuntu|debian) export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a ;;
        esac

        # Already root — just install
        if [ "$(id -u)" -eq 0 ]; then
            log_info "Installing ${pkgs[*]}..."
            if $install_cmd; then
                [ "$need_ffmpeg" = true ]  && HAS_FFMPEG=true  && log_success "ffmpeg installed"
                return 0
            fi
        # Passwordless sudo — just install
        elif command -v sudo &> /dev/null && sudo -n true 2>/dev/null; then
            log_info "Installing ${pkgs[*]}..."
            if sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a $install_cmd; then
                [ "$need_ffmpeg" = true ]  && HAS_FFMPEG=true  && log_success "ffmpeg installed"
                return 0
            fi
        # sudo needs password — ask once for everything
        elif command -v sudo &> /dev/null; then
            if [ "$IS_INTERACTIVE" = true ]; then
                echo ""
                log_info "sudo is needed ONLY to install optional system packages (${pkgs[*]}) via your package manager."
                log_info "Hermes Agent itself does not require or retain root access."
                if prompt_yes_no "Install ${description}? (requires sudo)" "no"; then
                    if sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a $install_cmd; then
                        [ "$need_ffmpeg" = true ]  && HAS_FFMPEG=true  && log_success "ffmpeg installed"
                        return 0
                    fi
                fi
            elif (: </dev/tty) 2>/dev/null; then
                # Non-interactive (e.g. curl | bash) but a terminal is available.
                # Read the prompt from /dev/tty (same approach the setup wizard uses).
                # Probe by actually opening /dev/tty: a bare existence test passes
                # in Docker builds where the device node is in the mount namespace
                # but opening fails with ENXIO. See #16746.
                echo ""
                log_info "sudo is needed ONLY to install optional system packages (${pkgs[*]}) via your package manager."
                log_info "Hermes Agent itself does not require or retain root access."
                if prompt_yes_no "Install ${description}?" "yes"; then
                    if sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a $install_cmd < /dev/tty; then
                        [ "$need_ffmpeg" = true ]  && HAS_FFMPEG=true  && log_success "ffmpeg installed"
                        return 0
                    fi
                fi
            else
                log_warn "Non-interactive mode and no terminal available — cannot install system packages"
                log_info "Install manually after setup completes: sudo $install_cmd"
            fi
        fi
    fi

    # ── Show manual instructions for anything still missing ──
    if [ "$HAS_FFMPEG" = false ] && [ "$need_ffmpeg" = true ]; then
        log_warn "ffmpeg not installed (TTS voice messages will be limited)"
        show_manual_install_hint "ffmpeg"
    fi
}

show_manual_install_hint() {
    local pkg="$1"
    log_info "To install $pkg manually:"
    case "$OS" in
        linux)
            case "$DISTRO" in
                ubuntu|debian) log_info "  sudo apt install $pkg" ;;
                fedora)        log_info "  sudo dnf install $pkg" ;;
                arch)          log_info "  sudo pacman -S $pkg"   ;;
                *)             log_info "  Use your package manager or visit the project homepage" ;;
            esac
            ;;
        android)
            log_info "  pkg install $pkg"
            ;;
        macos) log_info "  brew install $pkg" ;;
    esac
}

# ============================================================================
# Installation
# ============================================================================

clone_repo() {
    log_info "Installing to $INSTALL_DIR..."

    # An interrupted previous clone leaves a .git with no initial commit, where
    # the update path's `git stash` / `git checkout` abort with "You do not
    # have the initial commit yet" and fail the install (#40998). Move such a
    # partial checkout aside -- never delete it, in case it holds something the
    # user wants -- so the fresh-clone path below can proceed.
    if [ -d "$INSTALL_DIR/.git" ] && ! git -C "$INSTALL_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
        backup_dir="${INSTALL_DIR}.broken-$(date -u +%Y%m%d-%H%M%S)"
        log_warn "Existing checkout at $INSTALL_DIR has no commits (interrupted clone)."
        log_warn "Moving it aside to $backup_dir before re-cloning."
        mv "$INSTALL_DIR" "$backup_dir"
    fi

    if [ -d "$INSTALL_DIR" ]; then
        if [ -d "$INSTALL_DIR/.git" ]; then
            log_info "Existing installation found, updating..."
            cd "$INSTALL_DIR"

            local autostash_ref=""
            discard_update_lockfile_churn "$INSTALL_DIR"
            if [ -n "$(git status --porcelain)" ]; then
                # A previously interrupted update can leave the index with
                # unmerged entries. In that state `git stash` aborts with
                # "could not write index" and the later `git checkout` aborts
                # with "you need to resolve your current index first", failing
                # the whole install at the repository stage. Clear the conflict
                # markers with `git reset` first -- this keeps working-tree
                # changes (they're still stashed just below) and only drops the
                # index-level conflict state. Mirrors the `hermes update` path
                # (#4735).
                if [ -n "$(git ls-files --unmerged)" ]; then
                    log_info "Clearing unmerged index entries from a previous conflict..."
                    git reset -q
                fi
                local stash_name
                stash_name="hermes-install-autostash-$(date -u +%Y%m%d-%H%M%S)"
                log_info "Local changes detected, stashing before update..."
                git stash push --include-untracked -m "$stash_name"
                autostash_ref="stash@{0}"
            fi

            # Fetch only the target branch. A bare `git fetch origin` pulls
            # every ref, and this repo carries thousands of auto-generated
            # branches — on a non-single-branch checkout that turns each update
            # into a multi-minute download that can stall the installer.
            git remote set-branches origin "$BRANCH" 2>/dev/null || true
            git fetch origin "$BRANCH"
            git checkout "$BRANCH"
            # Managed installs should follow origin/$BRANCH exactly. If the
            # checkout has diverged (or has local-only commits), ff-only pull
            # cannot succeed — mirror ``hermes update`` and reset to the
            # fetched remote so bootstrap/install can recover.
            if ! git pull --ff-only origin "$BRANCH"; then
                log_warn "Fast-forward not possible; resetting managed install to origin/$BRANCH..."
                git reset --hard "origin/$BRANCH"
            fi

            if [ -n "$autostash_ref" ]; then
                local restore_now="yes"
                if [ -t 0 ] && [ -t 1 ]; then
                    echo
                    log_warn "Local changes were stashed before updating."
                    log_warn "Restoring them may reapply local customizations onto the updated codebase."
                    printf "Restore local changes now? [Y/n] "
                    read -r restore_answer
                    case "$restore_answer" in
                        ""|y|Y|yes|YES|Yes) restore_now="yes" ;;
                        *) restore_now="no" ;;
                    esac
                fi

                if [ "$restore_now" = "yes" ]; then
                    log_info "Restoring local changes..."
                    local restore_output=""
                    local restore_ok="yes"
                    if restore_output="$(git stash apply "$autostash_ref" 2>&1)"; then
                        restore_ok="yes"
                    else
                        restore_ok="no"
                    fi
                    local conflicted_files=""
                    conflicted_files="$(git diff --name-only --diff-filter=U || true)"
                    if [ "$restore_ok" = "yes" ] && [ -z "$conflicted_files" ]; then
                        git stash drop "$autostash_ref" >/dev/null
                        log_warn "Local changes were restored on top of the updated codebase."
                        log_warn "Review git diff / git status if Hermes behaves unexpectedly."
                    else
                        log_error "Update pulled new code, but restoring local changes hit conflicts."
                        if [ -n "$restore_output" ]; then
                            printf '%s\n' "$restore_output"
                        fi
                        if [ -n "$conflicted_files" ]; then
                            printf '\nConflicted files:\n'
                            while IFS= read -r file; do
                                [ -n "$file" ] && printf '  • %s\n' "$file"
                            done <<EOF
$conflicted_files
EOF
                        fi
                        printf '\n'
                        log_info "Your stashed changes are preserved — nothing is lost."
                        log_info "  Stash ref: $autostash_ref"
                        git reset --hard HEAD >/dev/null 2>&1 || true
                        log_info "Working tree reset to clean state."
                        log_info "Restore your changes later with: git stash apply $autostash_ref"
                    fi
                else
                    log_info "Skipped restoring local changes."
                    log_info "Your changes are still preserved in git stash."
                    log_info "Restore manually with: git stash apply $autostash_ref"
                fi
            fi
        else
            log_error "Directory exists but is not a git repository: $INSTALL_DIR"
            log_info "Remove it or choose a different directory with --dir"
            exit 1
        fi
    else
        # Try SSH first (for private repo access), fall back to HTTPS
        # GIT_SSH_COMMAND disables interactive prompts and sets a short timeout
        # so SSH fails fast instead of hanging when no key is configured.
        log_info "Trying SSH clone..."
        if GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=5" \
           git clone --depth 1 --branch "$BRANCH" "$REPO_URL_SSH" "$INSTALL_DIR" 2>/dev/null; then
            log_success "Cloned via SSH"
        else
            rm -rf "$INSTALL_DIR" 2>/dev/null  # Clean up partial SSH clone
            log_info "SSH failed, trying HTTPS..."
            if git clone --depth 1 --branch "$BRANCH" "$REPO_URL_HTTPS" "$INSTALL_DIR"; then
                log_success "Cloned via HTTPS"
            else
                log_error "Failed to clone repository"
                exit 1
            fi
        fi
    fi

    cd "$INSTALL_DIR"

    if [ -n "$INSTALL_COMMIT" ]; then
        # Validate the commit argument: must look like a hex SHA (full 40-char
        # or abbreviated 7-39 char). Reject anything else early so the user
        # gets a clear error instead of a misleading git message (#87268).
        if ! printf '%s' "$INSTALL_COMMIT" | grep -qE '^[0-9a-fA-F]{7,40}$'; then
            log_error "--commit expects a hex SHA (7-40 chars), got: $INSTALL_COMMIT"
            return 1
        fi
        # A commit pin must never move an existing install BACKWARDS. The
        # bootstrap installer bakes its build-time commit into the binary
        # (BUILD_PIN_COMMIT) and passes it as --commit on every install-mode
        # run -- including the one the desktop's failure screen retries. An
        # installer built months ago would otherwise rewind a current checkout
        # to its build commit, stranding the user on ancient code with a
        # current venv. Only pin when the target is not already an ancestor of
        # HEAD; a fresh clone has no such ancestry and pins normally.
        if ! git cat-file -e "$INSTALL_COMMIT^{commit}" 2>/dev/null; then
            if ! git fetch origin "$INSTALL_COMMIT"; then
                log_error "Could not fetch commit $INSTALL_COMMIT from origin."
                log_error "Abbreviated SHAs are not supported — use the full 40-char hash."
                log_error "Find it with: git ls-remote origin | grep <short-sha>"
                return 1
            fi
        fi
        if git rev-parse --verify --quiet HEAD >/dev/null 2>&1 \
           && git merge-base --is-ancestor "$INSTALL_COMMIT" HEAD 2>/dev/null \
           && [ "$(git rev-parse "$INSTALL_COMMIT^{commit}" 2>/dev/null)" != "$(git rev-parse HEAD)" ]; then
            if [ "$FORCE_COMMIT" = true ]; then
                log_warn "--force-commit: rolling this install back to $INSTALL_COMMIT."
                if ! git checkout --detach "$INSTALL_COMMIT"; then
                    log_error "Failed to detach at $INSTALL_COMMIT"
                    return 1
                fi
            else
                log_warn "Ignoring --commit $INSTALL_COMMIT: the checkout is already newer."
                log_warn "Pinning to it would roll this install back. Pass --force-commit to override."
            fi
        else
            log_info "Pinning checkout to commit $INSTALL_COMMIT..."
            if ! git checkout --detach "$INSTALL_COMMIT"; then
                log_error "Failed to detach at $INSTALL_COMMIT"
                return 1
            fi
        fi
    fi

    log_success "Repository ready"

    # The installer owns this checkout: record `updateMechanism: self` in
    # the stamp so the stamp-pure ladder (installation/tree.py) classifies
    # it as managed. Keep an existing stamp — re-running the installer on
    # an already-stamped install must not clobber richer provenance.
    if [ ! -f "$INSTALL_DIR/install-stamp.json" ]; then
        printf '{\n  "schemaVersion": 2,\n  "updateMechanism": "self",\n  "source": "installer"\n}\n' \
            > "$INSTALL_DIR/install-stamp.json"
        log_info "Wrote install stamp (updateMechanism: self)"
    fi
}

setup_venv() {
    if [ "$USE_VENV" = false ]; then
        log_info "Skipping virtual environment (--no-venv)"
        return 0
    fi

    log_info "Creating virtual environment with Python $PYTHON_VERSION..."

    if [ -d "venv" ]; then
        log_info "Virtual environment already exists, recreating..."
        rm -rf venv
    fi

    # uv creates the venv and pins the Python version in one step
    $UV_CMD venv venv --python "$PYTHON_VERSION"

    # Neutralize any inherited UV_PYTHON (e.g. UV_PYTHON=3.14 left in the
    # user's shell env). uv honours UV_PYTHON over an existing venv for the
    # later `uv sync` / `uv pip install` tiers, so without this it would
    # silently delete this 3.11 venv and recreate it at the inherited
    # version — building Rust transitives that have no wheel for that
    # version from source via maturin, which fails. Pinning UV_PYTHON to the
    # interpreter we just created forces every subsequent uv command onto it.
    if [ -x "$INSTALL_DIR/venv/bin/python" ]; then
        export UV_PYTHON="$INSTALL_DIR/venv/bin/python"
    fi

    log_success "Virtual environment ready (Python $PYTHON_VERSION)"
}

install_deps() {
    log_info "Installing dependencies..."

    # Re-pin UV_PYTHON to the venv interpreter. setup_venv already does this,
    # but the bootstrap runs install stages (`venv`, `python-deps`) as separate
    # processes, so an export from setup_venv does NOT survive into a separate
    # python-deps invocation. Re-deriving it here covers that path. Without it,
    # an inherited UV_PYTHON=3.14 makes the uv sync/pip tiers below recreate the
    # venv at 3.14 and fail the maturin source build (no cp314 wheels yet).
    if [ -x "$INSTALL_DIR/venv/bin/python" ]; then
        export UV_PYTHON="$INSTALL_DIR/venv/bin/python"
    fi

    if [ "$USE_VENV" = true ]; then
        # Tell uv to install into our venv (no need to activate)
        export VIRTUAL_ENV="$INSTALL_DIR/venv"
    fi

    # On Debian/Ubuntu (including WSL), some Python packages need build tools.
    # Check and offer to install them if missing.
    if [ "$DISTRO" = "ubuntu" ] || [ "$DISTRO" = "debian" ]; then
        local need_build_tools=false
        for pkg in gcc python3-dev libffi-dev; do
            if ! dpkg -s "$pkg" &>/dev/null; then
                need_build_tools=true
                break
            fi
        done
        if [ "$need_build_tools" = true ]; then
            log_info "Some build tools may be needed for Python packages..."
            if command -v sudo &> /dev/null; then
                if sudo -n true 2>/dev/null; then
                    sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq build-essential python3-dev libffi-dev >/dev/null 2>&1 || true
                    log_success "Build tools installed"
                else
                    log_info "sudo is needed ONLY to install build tools (build-essential, python3-dev, libffi-dev) via apt."
                    log_info "Hermes Agent itself does not require or retain root access."
                    if prompt_yes_no "Install build tools?" "yes"; then
                        sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq build-essential python3-dev libffi-dev >/dev/null 2>&1 || true
                        log_success "Build tools installed"
                    fi
                fi
            fi
        fi
    fi

    # Install the main package in editable mode with all extras.
    #
    # Hash-verified install (Tier 0) — when uv.lock is present, prefer
    # `uv sync --locked`. The lockfile records SHA256 hashes for every
    # transitive, so a compromised transitive (different hash than what
    # we shipped) is REJECTED by the resolver. This is the *only* path
    # that protects against the "direct dep is fine, but the dep's dep
    # got worm-poisoned overnight" failure mode. All `uv pip install`
    # tiers below re-resolve transitives fresh from PyPI without any
    # hash verification — they exist to keep installs working when the
    # lockfile is stale, missing, or out-of-sync with the current
    # extras spec, NOT because they're equivalent in posture.
    if [ -f "uv.lock" ]; then
        log_info "Trying tier: hash-verified (uv.lock) ..."
        log_info "(this resolves + downloads the curated [all] set — first run on a"
        log_info " fresh venv can take 1-5 minutes; uv prints progress below)"
        # Stream uv's progress directly to the user instead of swallowing
        # it with `2>"$(mktemp)"`.  Two reasons:
        #   1. `--extra all --locked` against a fresh venv has to pull
        #      every transitive — silencing stderr makes the install
        #      look frozen for minutes on slow networks. Users see
        #      "Trying tier: hash-verified ..." and assume it's hung.
        #   2. The previous `2>"$(mktemp)"` substituted the path at
        #      command-build time but never saved it, so on failure the
        #      uv error message was unreachable — the user just got the
        #      generic "lockfile may be stale" warning.
        #
        # Critical flag choice: `--extra all`, NOT `--all-extras`.
        #   --all-extras = every [project.optional-dependencies] key.
        #                  This bypasses the curated `[all]` extra
        #                  entirely and pulls e.g. [matrix] (which
        #                  needs python-olm + make on Windows) and
        #                  [rl] (git+https deps that fail offline).
        #   --extra all  = install just the `[all]` extra's contents.
        #                  This respects the curation in pyproject.toml.
        # uv's own progress UI handles TTY detection and downgrades
        # gracefully when stdout/stderr aren't terminals.
        if UV_PROJECT_ENVIRONMENT="$INSTALL_DIR/venv" $UV_CMD sync --extra all --locked; then
            log_success "Main package installed (hash-verified via uv.lock)"
            log_success "All dependencies installed"
            return 0
        fi
        log_warn "uv.lock sync failed (see uv output above), falling back to PyPI resolve..."
    else
        log_info "uv.lock not found — falling back to PyPI resolve (no hash verification)"
    fi

    # Multi-tier fallback. The point of the tiers is that ONE compromised
    # PyPI package (a worm-poisoned release that gets quarantined, like
    # mistralai 2.4.6 in May 2026) shouldn't be able to silently demote a
    # fresh install all the way down to "core only" — the user should keep
    # everything else they signed up for.
    #
    # Tier 1: [all] — the curated extra in pyproject.toml.
    # Tier 2: [all] minus the currently-broken extras list (_BROKEN_EXTRAS).
    #         Edit _BROKEN_EXTRAS below when something on PyPI breaks; this
    #         lets users keep the rest of [all] when one transitive is
    #         unavailable. The list of [all]'s contents is parsed from
    #         pyproject.toml at runtime — there is NO hand-mirrored copy
    #         to drift out of sync. If you want to change what [all]
    #         contains, edit pyproject.toml only.
    # Tier 3: bare `.` — last-resort so at least the core CLI launches.
    #         Skipped tiers like "PyPI-only extras (no git deps)" used to
    #         exist to dodge [rl] / [matrix] git+sdist deps; those are no
    #         longer in [all] post-2026-05-12 lazy-install migration, so
    #         a separate PyPI-only tier had no remaining content.
    local _BROKEN_EXTRAS=()  # populate when an extra becomes unresolvable

    # Parse [project.optional-dependencies].all from pyproject.toml.
    # tomllib is stdlib on Python 3.11+ which uv's bootstrap guarantees.
    # Falls back to a hand list if parse fails — defensive only.
    local _ALL_EXTRAS_CSV
    _ALL_EXTRAS_CSV="$(
        "$PYTHON_PATH" - <<'PY' 2>/dev/null
import re, sys, tomllib
try:
    with open("pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    specs = data["project"]["optional-dependencies"]["all"]
    extras = []
    for s in specs:
        m = re.search(r"hermes-agent\[([\w-]+)\]", s)
        if m:
            extras.append(m.group(1))
    print(",".join(extras))
except Exception as e:
    print("", file=sys.stderr)
    sys.exit(1)
PY
    )"
    if [ -z "$_ALL_EXTRAS_CSV" ]; then
        log_warn "Could not parse [all] from pyproject.toml; falling back to .[all] only."
        _ALL_EXTRAS_CSV=""
    fi

    # Build "[all] minus broken" spec by filtering the parsed list.
    local _SAFE_SPEC=".[all]"
    if [ -n "$_ALL_EXTRAS_CSV" ] && [ "${#_BROKEN_EXTRAS[@]}" -gt 0 ]; then
        local _SAFE_EXTRAS=()
        local _e _b _skip
        IFS=',' read -ra _ALL_EXTRAS_ARR <<< "$_ALL_EXTRAS_CSV"
        for _e in "${_ALL_EXTRAS_ARR[@]}"; do
            _skip=false
            for _b in "${_BROKEN_EXTRAS[@]}"; do
                if [ "$_e" = "$_b" ]; then _skip=true; break; fi
            done
            if [ "$_skip" = false ]; then _SAFE_EXTRAS+=("$_e"); fi
        done
        _SAFE_SPEC=".[$(IFS=,; echo "${_SAFE_EXTRAS[*]}")]"
    fi

    ALL_INSTALL_LOG=$(mktemp)
    local _installed=false
    local _tier_name=""

    install_tier() {
        local name="$1"; local spec="$2"
        log_info "Trying tier: $name ..."
        if $UV_CMD pip install -e "$spec" 2>"$ALL_INSTALL_LOG"; then
            log_success "Main package installed ($name)"
            _installed=true
            _tier_name="$name"
            return 0
        fi
        log_warn "Tier '$name' failed. Top of pip output:"
        head -5 "$ALL_INSTALL_LOG" | sed 's/^/    /' >&2
        return 1
    }

    install_tier "all" ".[all]" \
        || install_tier "all minus known-broken (${_BROKEN_EXTRAS[*]:-none})" "$_SAFE_SPEC" \
        || install_tier "core only (no extras)" "."

    rm -f "$ALL_INSTALL_LOG"

    if [ "$_installed" = false ]; then
        log_error "Package installation failed even with no extras."
        log_info "Check that build tools are installed: sudo apt install build-essential python3-dev"
        log_info "Then re-run: cd $INSTALL_DIR && uv pip install -e '.[all]'"
        exit 1
    fi

    if [ "$_tier_name" != "all" ]; then
        log_warn "Note: installed via fallback tier ($_tier_name)."
        log_info "Some optional features may be missing. After resolving any"
        log_info "PyPI/network issue, re-run: $UV_CMD pip install -e '.[all]'"
    fi

    log_success "Main package installed"

    log_success "All dependencies installed"
}

setup_path() {
    log_info "Setting up hermes command..."

    if [ "$USE_VENV" = true ]; then
        HERMES_BIN="$INSTALL_DIR/venv/bin/python"
        HERMES_ENTRYPOINT="$INSTALL_DIR/hermes"
    else
        HERMES_BIN="$(which hermes 2>/dev/null || echo "")"
        if [ -z "$HERMES_BIN" ]; then
            log_warn "hermes not found on PATH after install"
            return 0
        fi
    fi

    # Verify the interpreter and the checked-in entrypoint needed by the launcher.
    if [ ! -x "$HERMES_BIN" ] || { [ "$USE_VENV" = true ] && [ ! -f "$HERMES_ENTRYPOINT" ]; }; then
        log_warn "Hermes launcher prerequisites not found"
        log_info "This usually means the Python package install didn't complete successfully."
        log_info "Try: cd $INSTALL_DIR && uv pip install -e '.[all]'"
        return 0
    fi

    local command_link_dir
    local command_link_display_dir
    command_link_dir="$(get_command_link_dir)"
    command_link_display_dir="$(get_command_link_display_dir)"

    # Create a user-facing shim for the hermes command.
    # We intentionally clear PYTHONPATH/PYTHONHOME here so inherited env vars
    # can't make this launcher import modules from another checkout.
    mkdir -p "$command_link_dir"
    # Older installs created this path as a symlink to $HERMES_BIN. Without
    # the rm, `cat >` follows the symlink and overwrites the venv pip entry
    # point with this shim — making `exec "$HERMES_BIN"` self-recurse. (#21454)
    rm -f "$command_link_dir/hermes"
    if [ "$USE_VENV" = true ]; then
        # uv-generated console scripts resolve themselves through `realpath`,
        # which stock macOS does not provide. Run the checked-in entrypoint
        # with the venv interpreter instead, so the public launcher remains
        # independent of non-standard shell utilities.
        cat > "$command_link_dir/hermes" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
exec "$HERMES_BIN" "$HERMES_ENTRYPOINT" "\$@"
EOF
    else
        cat > "$command_link_dir/hermes" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
exec "$HERMES_BIN" "\$@"
EOF
    fi
    chmod +x "$command_link_dir/hermes"
    log_success "Installed hermes launcher → $command_link_display_dir/hermes"

    # Also expose `hermes-agent`. The `hermes-agent` console script declared in
    # pyproject.toml's [project.scripts] lives inside the venv, which is not on
    # the login-shell PATH. Without this launcher users can't invoke the agent
    # entrypoint directly from outside the venv. (#74819)
    rm -f "$command_link_dir/hermes-agent"
    if [ "$USE_VENV" = true ]; then
        cat > "$command_link_dir/hermes-agent" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
exec "$HERMES_BIN" "$INSTALL_DIR/run_agent.py" "\$@"
EOF
    else
        cat > "$command_link_dir/hermes-agent" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
exec "$HERMES_BIN" run_agent.py "\$@"
EOF
    fi
    chmod +x "$command_link_dir/hermes-agent"
    log_success "Installed hermes-agent launcher → $command_link_display_dir/hermes-agent"

    # Also expose `hermes-acp`. ACP hosts (Zed, JetBrains, Buzz) resolve the
    # agent by command name on the login-shell PATH, and the `hermes-acp`
    # console script lives inside the venv, which is not on that PATH. Without
    # this launcher those hosts report Hermes as not installed. (#21454 applies
    # here too: clear the path first so `cat >` cannot follow an old symlink
    # into the venv and overwrite the console script.)
    rm -f "$command_link_dir/hermes-acp"
    if [ "$USE_VENV" = true ]; then
        cat > "$command_link_dir/hermes-acp" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
exec "$HERMES_BIN" "$HERMES_ENTRYPOINT" acp "\$@"
EOF
    else
        cat > "$command_link_dir/hermes-acp" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
exec "$HERMES_BIN" acp "\$@"
EOF
    fi
    chmod +x "$command_link_dir/hermes-acp"
    log_success "Installed hermes-acp launcher → $command_link_display_dir/hermes-acp"

    # FHS layout: /usr/local/bin is normally on PATH for login shells (via
    # /etc/profile pathmunge), but on RHEL/CentOS/Rocky/Alma 8+ non-login
    # interactive root shells (su, sudo -s, tmux panes, some web terminals)
    # only source /etc/bashrc, which does NOT add /usr/local/bin — and
    # /root/.bash_profile doesn't either.  So verify with `command -v` and
    # fall back to writing a PATH guard into /root/.bashrc when needed.
    if [ "$ROOT_FHS_LAYOUT" = true ]; then
        export PATH="$command_link_dir:$PATH"
        # Probe a fresh non-login interactive bash the way the user will use it.
        # `bash -i -c` sources ~/.bashrc but NOT ~/.bash_profile or /etc/profile,
        # which is the exact scenario where RHEL root loses /usr/local/bin.
        if env -i HOME="$HOME" TERM="${TERM:-dumb}" bash -i -c 'command -v hermes' \
                >/dev/null 2>&1; then
            log_info "/usr/local/bin is already on PATH for all shells"
            log_success "hermes command ready"
            return 0
        fi

        log_info "hermes not on PATH in non-login shells (common on RHEL-family)"
        PATH_LINE='export PATH="/usr/local/bin:$PATH"'
        PATH_COMMENT='# Hermes Agent — ensure /usr/local/bin is on PATH (RHEL non-login shells)'
        for SHELL_CONFIG in "$HOME/.bashrc" "$HOME/.bash_profile"; do
            [ -f "$SHELL_CONFIG" ] || continue
            if ! grep -v '^[[:space:]]*#' "$SHELL_CONFIG" 2>/dev/null \
                    | grep -qE 'PATH=.*(/usr/local/bin|\$command_link_dir)'; then
                echo "" >> "$SHELL_CONFIG"
                echo "$PATH_COMMENT" >> "$SHELL_CONFIG"
                echo "$PATH_LINE" >> "$SHELL_CONFIG"
                log_success "Added /usr/local/bin to PATH in $SHELL_CONFIG"
            fi
        done
        log_success "hermes command ready"
        return 0
    fi

    # Check if ~/.local/bin is on PATH; if not, add it to shell config.
    # Detect the user's actual login shell (not the shell running this script,
    # which is always bash when piped from curl).
    if ! echo "$PATH" | tr ':' '\n' | grep -q "^$command_link_dir$"; then
        SHELL_CONFIGS=()
        IS_FISH=false
        LOGIN_SHELL="$(basename "${SHELL:-/bin/bash}")"
        case "$LOGIN_SHELL" in
            zsh)
                [ -f "$HOME/.zshrc" ] && SHELL_CONFIGS+=("$HOME/.zshrc")
                [ -f "$HOME/.zprofile" ] && SHELL_CONFIGS+=("$HOME/.zprofile")
                # If neither exists, create ~/.zshrc (common on fresh macOS installs)
                if [ ${#SHELL_CONFIGS[@]} -eq 0 ]; then
                    touch "$HOME/.zshrc"
                    SHELL_CONFIGS+=("$HOME/.zshrc")
                fi
                ;;
            bash)
                [ -f "$HOME/.bashrc" ] && SHELL_CONFIGS+=("$HOME/.bashrc")
                [ -f "$HOME/.bash_profile" ] && SHELL_CONFIGS+=("$HOME/.bash_profile")
                ;;
            fish)
                # fish uses ~/.config/fish/config.fish and fish_add_path — not export PATH=
                IS_FISH=true
                FISH_CONFIG="$HOME/.config/fish/config.fish"
                mkdir -p "$(dirname "$FISH_CONFIG")"
                touch "$FISH_CONFIG"
                ;;
            *)
                [ -f "$HOME/.bashrc" ] && SHELL_CONFIGS+=("$HOME/.bashrc")
                [ -f "$HOME/.zshrc" ] && SHELL_CONFIGS+=("$HOME/.zshrc")
                ;;
        esac
        # Also ensure ~/.profile has it (sourced by login shells on
        # Ubuntu/Debian/WSL even when ~/.bashrc is skipped)
        [ "$IS_FISH" = "false" ] && [ -f "$HOME/.profile" ] && SHELL_CONFIGS+=("$HOME/.profile")

        PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'

        for SHELL_CONFIG in "${SHELL_CONFIGS[@]}"; do
            if ! grep -v '^[[:space:]]*#' "$SHELL_CONFIG" 2>/dev/null | grep -qE 'PATH=.*\.local/bin'; then
                echo "" >> "$SHELL_CONFIG"
                echo "# Hermes Agent — ensure ~/.local/bin is on PATH" >> "$SHELL_CONFIG"
                echo "$PATH_LINE" >> "$SHELL_CONFIG"
                log_success "Added ~/.local/bin to PATH in $SHELL_CONFIG"
            fi
        done

        # fish uses fish_add_path instead of export PATH=...
        if [ "$IS_FISH" = "true" ]; then
            if ! grep -q 'fish_add_path.*\.local/bin' "$FISH_CONFIG" 2>/dev/null; then
                echo "" >> "$FISH_CONFIG"
                echo "# Hermes Agent — ensure ~/.local/bin is on PATH" >> "$FISH_CONFIG"
                echo 'fish_add_path "$HOME/.local/bin"' >> "$FISH_CONFIG"
                log_success "Added ~/.local/bin to PATH in $FISH_CONFIG"
            fi
        fi

        if [ "$IS_FISH" = "false" ] && [ ${#SHELL_CONFIGS[@]} -eq 0 ]; then
            log_warn "Could not detect shell config file to add ~/.local/bin to PATH"
            log_info "Add manually: $PATH_LINE"
        fi
    else
        log_info "~/.local/bin already on PATH"
    fi

    # Export for current session so hermes works immediately
    export PATH="$command_link_dir:$PATH"

    log_success "hermes command ready"
}

copy_config_templates() {
    log_info "Setting up configuration files..."

    # Create ~/.hermes directory structure (config at top level, code in subdir)
    mkdir -p "$HERMES_HOME"/{cron,sessions,logs,pairing,hooks,image_cache,audio_cache,memories,skills}

    # Create .env at ~/.hermes/.env (top level, easy to find)
    if [ ! -f "$HERMES_HOME/.env" ]; then
        if [ -f "$INSTALL_DIR/.env.example" ]; then
            cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env"
            log_success "Created ~/.hermes/.env from template"
        else
            touch "$HERMES_HOME/.env"
            log_success "Created ~/.hermes/.env"
        fi
    else
        log_info "~/.hermes/.env already exists, keeping it"
    fi
    # Restrict .env permissions — this file holds API keys and tokens.
    # 0600 ensures only the file owner can read/write, matching standard
    # practice for credential files (.netrc, .aws/credentials, .ssh/config).
    chmod 600 "$HERMES_HOME/.env"
    configure_browser_env_from_system_browser

    # Create config.yaml at ~/.hermes/config.yaml (top level, easy to find)
    if [ ! -f "$HERMES_HOME/config.yaml" ]; then
        if [ -f "$INSTALL_DIR/cli-config.yaml.example" ]; then
            cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"
            log_success "Created ~/.hermes/config.yaml from template"
        fi
    else
        log_info "~/.hermes/config.yaml already exists, keeping it"
    fi

    # Create SOUL.md if it doesn't exist (global persona file).
    # This MUST match DEFAULT_SOUL_MD in hermes_cli/default_soul.py — the
    # runtime (_ensure_default_soul_md) treats the old comment-only scaffold as
    # "never customized" and upgrades it to this text on next run, so any drift
    # here is self-healing, but keep them in sync to avoid a churn on first run.
    if [ ! -f "$HERMES_HOME/SOUL.md" ]; then
        cat > "$HERMES_HOME/SOUL.md" << 'SOUL_EOF'
You are Hermes Agent, an intelligent AI assistant created by Nous Research. You are helpful, knowledgeable, and direct. You assist users with a wide range of tasks including answering questions, writing and editing code, analyzing information, creative work, and executing actions via your tools. You communicate clearly, admit uncertainty when appropriate, and prioritize being genuinely useful over being verbose unless otherwise directed below. Be targeted and efficient in your exploration and investigations.
SOUL_EOF
        log_success "Created ~/.hermes/SOUL.md (edit to customize personality)"
    fi

    log_success "Configuration directory ready: ~/.hermes/"

    # Seed bundled skills into ~/.hermes/skills/ (manifest-based, one-time per skill)
    if [ "$NO_SKILLS" = true ]; then
        # Blank-slate install: write the opt-out marker and skip seeding.
        # skills_sync.py and `hermes update` both honor this marker, so the
        # default profile stays empty across future updates too.
        printf '%s\n' \
            "This profile opted out of bundled-skill seeding (installed with --no-skills)." \
            "Delete this file to re-enable sync on the next 'hermes update'." \
            > "$HERMES_HOME/.no-bundled-skills" 2>/dev/null || true
        log_info "Skipping bundled skills (--no-skills). Wrote $HERMES_HOME/.no-bundled-skills"
        log_info "  Future 'hermes update' runs will not inject bundled skills. Delete the marker to opt back in."
    else
        log_info "Syncing bundled skills to ~/.hermes/skills/ ..."
        if "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/tools/skills_sync.py" 2>/dev/null; then
            log_success "Skills synced to ~/.hermes/skills/"
        else
            # Fallback: simple directory copy if Python sync fails
            if [ -d "$INSTALL_DIR/skills" ] && [ ! "$(ls -A "$HERMES_HOME/skills/" 2>/dev/null | grep -v '.bundled_manifest')" ]; then
                cp -r "$INSTALL_DIR/skills/"* "$HERMES_HOME/skills/" 2>/dev/null || true
                log_success "Skills copied to ~/.hermes/skills/"
            fi
        fi
    fi
}

find_system_browser() {
    # Honor ONLY an explicit, user-set AGENT_BROWSER_EXECUTABLE_PATH override.
    #
    # We deliberately do NOT scan PATH or well-known app locations any more.
    # Auto-detection silently bound the install to whatever `command -v chromium`
    # resolved to — most damagingly a Snap Chromium (/snap/bin/chromium), whose
    # sandbox blocks agent-browser's control socket under /tmp, so every
    # browser_navigate hung until the 60s timeout fired ("opening web page
    # failed"). Every install now uses the bundled Playwright Chromium unless the
    # user explicitly points elsewhere.
    local override="${AGENT_BROWSER_EXECUTABLE_PATH:-}"

    if [ -z "$override" ]; then
        return 1
    fi

    # A Snap binary is never a valid target — its confinement is the very bug we
    # are fixing — so reject it even when set explicitly.
    case "$override" in
        /snap/*) return 1 ;;
    esac

    if [ -x "$override" ]; then
        echo "$override"
        return 0
    fi
    if command -v "$override" >/dev/null 2>&1; then
        command -v "$override"
        return 0
    fi

    return 1
}

strip_snap_browser_override() {
    # Existing installs created before the system-browser fallback was dropped
    # may carry an auto-written AGENT_BROWSER_EXECUTABLE_PATH pointing at a Snap
    # Chromium (/snap/bin/chromium). That path is the root cause of the "opening
    # web page failed" hang, and the runtime reads it straight from .env — so
    # removing the fallback in the installer is not enough on its own. Strip any
    # snap-pointing override here (and its auto-written comment) so the bundled
    # Chromium download runs and the agent stops using the broken binary. A
    # deliberately-set non-snap override is left untouched.
    local env_file="$HERMES_HOME/.env"

    [ -f "$env_file" ] || return 0
    grep -Eq '^AGENT_BROWSER_EXECUTABLE_PATH=/snap/' "$env_file" 2>/dev/null || return 0

    local tmp
    tmp="$(mktemp)" || return 0
    if grep -Ev '^AGENT_BROWSER_EXECUTABLE_PATH=/snap/|^# Hermes Agent browser tools' "$env_file" > "$tmp"; then
        mv "$tmp" "$env_file"
        log_warn "Removed stale Snap browser override (AGENT_BROWSER_EXECUTABLE_PATH=/snap/...) from $env_file"
        log_info "Hermes will use the bundled Chromium instead."
        # Drop it from this process too so the rest of the run doesn't re-detect it.
        unset AGENT_BROWSER_EXECUTABLE_PATH
    else
        rm -f "$tmp"
    fi
}

run_browser_install_with_timeout() {
    run_with_timeout "$@"
}

# Run a command with a hard wall-clock timeout, returning non-zero if it is
# killed. Prefers GNU coreutils `timeout` (Linux) or `gtimeout` (macOS via
# Homebrew) for an external-command target; otherwise (and always for a shell
# function target, which the `timeout` binary cannot exec) it uses a pure-shell
# watchdog: launch the command in its own process group, poll until it finishes,
# and SIGTERM (then SIGKILL) the whole group on timeout. The pure-shell path is
# what protects the bug-#39219 case — a stalled Electron download on macOS,
# where `timeout` is usually absent — turning an indefinite hang into a non-zero
# exit so callers (install_desktop) can self-heal via the mirror fallback.
#
# $1 (timeout) must be a bare integer number of seconds — the pure-shell loop
# compares it arithmetically (the `timeout` binary would also accept suffixes
# like 15m, but we normalize so both paths share one contract). On timeout the
# return code is 124, matching GNU `timeout`.
run_with_timeout() {
    local timeout_seconds="$1"
    shift

    # Normalize to a bare integer; fall back to the desktop default if a caller
    # ever passes a suffixed/empty value (the pure-shell loop needs an int).
    case "$timeout_seconds" in
        ''|*[!0-9]*) timeout_seconds=900 ;;
    esac

    # The `timeout` binary can only exec an external command, not a shell
    # function. Use it only when the target is NOT a function; functions always
    # go through the pure-shell watchdog (which runs them in a subshell of the
    # current shell and sees them directly — no fragile env export needed).
    if [ "$(type -t "$1" 2>/dev/null)" != "function" ]; then
        local timeout_bin=""
        if command -v timeout >/dev/null 2>&1; then
            timeout_bin="timeout"
        elif command -v gtimeout >/dev/null 2>&1; then
            timeout_bin="gtimeout"
        fi
        if [ -n "$timeout_bin" ]; then
            # GNU `timeout` runs the command in its own process group, so a
            # terminal Ctrl+C is delivered to `timeout` but never reaches the
            # child — the download looks frozen and ignores Ctrl+C (#35166).
            # `--foreground` keeps the command in the shell's foreground group
            # so Ctrl+C reaches it; `-k 10` sends SIGKILL 10s after the deadline
            # so a wedged download can't outlive the timeout. Both flags are
            # GNU-only — probe once and fall back to plain `timeout` on BusyBox
            # (Alpine). When neither binary exists (stock macOS) we drop to the
            # pure-shell watchdog below.
            if "$timeout_bin" --foreground -k 10 1 true >/dev/null 2>&1; then
                "$timeout_bin" --foreground -k 10 "$timeout_seconds" "$@"
            else
                "$timeout_bin" "$timeout_seconds" "$@"
            fi
            return $?
        fi
    fi

    # Pure-shell fallback: run in a new process group so we can kill the whole
    # subtree (npm spawns node + the Electron downloader as children).
    set -m
    ( "$@" ) &
    local cmd_pid=$!
    set +m

    local waited=0
    local rc
    while [ "$waited" -lt "$timeout_seconds" ]; do
        if ! kill -0 "$cmd_pid" 2>/dev/null; then
            # `|| rc=$?` keeps the non-zero child status without letting `set -e`
            # abort the caller here (this would fire if run_with_timeout were
            # ever called outside an if/|| context).
            rc=0; wait "$cmd_pid" 2>/dev/null || rc=$?
            return "$rc"
        fi
        sleep 1
        waited=$((waited + 1))
    done

    # Final boundary recheck: the command may have finished during the last
    # poll interval — don't kill (and mislabel as 124) a process that already
    # exited cleanly in the last second of the budget.
    if ! kill -0 "$cmd_pid" 2>/dev/null; then
        rc=0; wait "$cmd_pid" 2>/dev/null || rc=$?
        return "$rc"
    fi

    # Timed out: kill the process group (negative PID), escalate to KILL.
    kill -TERM "-$cmd_pid" 2>/dev/null || kill -TERM "$cmd_pid" 2>/dev/null || true
    sleep 2
    kill -KILL "-$cmd_pid" 2>/dev/null || kill -KILL "$cmd_pid" 2>/dev/null || true
    wait "$cmd_pid" 2>/dev/null || true
    return 124
}

# Return success only when the host is an apt release NEWER than the newest one
# Playwright's platform resolver recognizes — the exact condition that makes
# `playwright install` hang uninterruptibly (#35166). We scope the override
# retry to this case rather than retrying on *any* failure, so a genuine
# network/disk/permission failure doesn't get a mismatched-glibc build forced
# onto it. Newest Playwright-known apt releases as of this writing: Ubuntu
# 24.04, Debian 13. Anything above triggers the fallback; everything Playwright
# already handles (and every non-apt distro) does not.
playwright_host_unrecognized() {
    # Compare dotted versions: returns 0 if $1 > $2.
    _ver_gt() {
        [ "$1" = "$2" ] && return 1
        [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" = "$1" ]
    }
    case "$DISTRO" in
        ubuntu) _ver_gt "${DISTRO_VERSION:-0}" "24.04" ;;
        debian) _ver_gt "${DISTRO_VERSION:-0}" "13" ;;
        *) return 1 ;;  # Non-apt or unknown — not the #35166 hang condition.
    esac
}

# Compute the PLAYWRIGHT_HOST_PLATFORM_OVERRIDE value to retry an install with
# when Playwright's platform resolver rejects the host. ubuntu24.04 is the
# newest Linux build Playwright has shipped across recent releases and runs on
# newer apt releases (its binaries are dynamically linked); we point too-new /
# unrecognized hosts at it. Only x64/arm64 Linux have Playwright builds — emit
# nothing for anything else so the caller skips the retry. Echoes the value
# (e.g. "ubuntu24.04-x64") or nothing.
playwright_fallback_platform() {
    case "$(uname -m)" in
        x86_64|amd64) echo "ubuntu24.04-x64" ;;
        aarch64|arm64) echo "ubuntu24.04-arm64" ;;
        *) : ;;  # No Playwright Linux build for this arch.
    esac
}

# Run a `playwright install ...` command, and if it fails or hangs (the
# uninterruptible "Installing Playwright Chromium with system dependencies"
# stall on apt releases Playwright doesn't recognize yet — Ubuntu 26.04,
# Debian 14, future distros — see #35166), retry it ONCE with
# PLAYWRIGHT_HOST_PLATFORM_OVERRIDE pinned to the newest known build.
#
# The override retry is scoped to the actual hang condition: it fires only when
# the host is an apt release NEWER than Playwright recognizes
# (playwright_host_unrecognized). On every release Playwright already supports
# (Ubuntu <=24.04, Debian <=13) and every non-apt distro, the first attempt is
# authoritative and a failure is reported as-is — we never force a
# mismatched-glibc build (microsoft/playwright#35114) onto a host Playwright
# handles correctly. This is deliberately narrower than a retry-on-any-failure:
# a network/disk/permission error on a supported host should surface, not get
# papered over with a platform override. Playwright's maintainers bless this
# env var as the supported escape hatch for unrecognized platforms
# (microsoft/playwright#33434); a hardcoded full distro/version table was
# rejected upstream (microsoft/playwright#33432), so we only need the
# newest-known floor here.
#
# An operator-provided PLAYWRIGHT_HOST_PLATFORM_OVERRIDE is always respected:
# it is inherited by the first attempt, and the retry is skipped.
#
# Usage: run_playwright_install <timeout_seconds> npx playwright install [args...]
run_playwright_install() {
    local timeout_seconds="$1"
    shift

    # First attempt: native platform resolution (inherits any operator override).
    if run_browser_install_with_timeout "$timeout_seconds" "$@" 2>/dev/null; then
        return 0
    fi

    # Operator already pinned the platform — their choice already applied to the
    # attempt above; a second identical run won't help.
    if [ -n "${PLAYWRIGHT_HOST_PLATFORM_OVERRIDE:-}" ]; then
        return 1
    fi

    # Only retry with an override on the apt releases too new for Playwright to
    # recognize (the #35166 hang). Any other failure is a real failure and is
    # surfaced unchanged.
    if ! playwright_host_unrecognized; then
        return 1
    fi

    local fallback
    fallback="$(playwright_fallback_platform)"
    if [ -z "$fallback" ]; then
        return 1  # No usable fallback build for this arch.
    fi

    log_warn "Playwright doesn't recognize ${DISTRO} ${DISTRO_VERSION} yet — retrying with PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=$fallback"
    log_info "(apt releases newer than Playwright knows hang at this step; see #35166)"
    PLAYWRIGHT_HOST_PLATFORM_OVERRIDE="$fallback" \
        run_browser_install_with_timeout "$timeout_seconds" "$@"
}

configure_browser_env_from_system_browser() {
    local env_file="$HERMES_HOME/.env"
    local browser_path="${DETECTED_BROWSER_EXECUTABLE:-}"

    if [ -z "$browser_path" ]; then
        browser_path="$(find_system_browser 2>/dev/null || true)"
    fi

    if [ -z "$browser_path" ]; then
        return 0
    fi

    mkdir -p "$HERMES_HOME"
    if [ ! -f "$env_file" ]; then
        touch "$env_file"
    fi

    if grep -q '^AGENT_BROWSER_EXECUTABLE_PATH=' "$env_file" 2>/dev/null; then
        log_info "AGENT_BROWSER_EXECUTABLE_PATH already configured"
        return 0
    fi

    {
        echo ""
        echo "# Hermes Agent browser tools — explicit browser override."
        echo "AGENT_BROWSER_EXECUTABLE_PATH=$browser_path"
    } >> "$env_file"
    log_success "Configured browser tools to use $browser_path"
}

install_node_deps() {
    if [ -f "$INSTALL_DIR/package.json" ]; then
        log_info "Installing Node.js dependencies (browser tools)..."
        cd "$INSTALL_DIR"
        # Time-boxed: a stalled registry fetch would otherwise hang here with no
        # progress (same #39219 stall class as the desktop build below).
        # A failed npm install used to still print "✓ Node.js dependencies
        # installed", hiding the degradation from the user (#77003). Now it
        # fails the install outright instead of burying the warning (#85297).
        # Capture npm output so failures are diagnosable (#87340).
        local npm_log
        npm_log="$(mktemp)"
        if ! run_with_timeout "$NODE_DEPS_TIMEOUT" npm install --silent \
                >"$npm_log" 2>&1; then
            log_error "npm install failed or timed out; Node.js dependencies were not installed"
            if [ -s "$npm_log" ]; then
                log_error "npm output:"
                cat "$npm_log" >&2
            fi
            rm -f "$npm_log"
            restore_dirty_lockfiles "$INSTALL_DIR"
            return 1
        fi
        rm -f "$npm_log"
        log_success "Node.js dependencies installed"

        # Chromium system LIBRARIES. The browser BINARY itself is staged by
        # the provisioner sweep above (--extra agent-browser expands to the
        # pinned Chromium pair via the pin table's requires edge), so the
        # download half of the old lane is gone — this lane now owns only
        # the root-privileged OS package work a pinned browser still needs
        # (a staged Chromium without libnss3 does not launch any more than
        # a downloaded one did; nix/runtime-pins.nix runs autoPatchelfHook
        # for exactly this reason).
        if [ "$SKIP_BROWSER" = true ]; then
            log_info "Skipping browser setup (--skip-browser)"
            log_info "Browser tools will be unavailable until you run manually:"
            log_info "  cd $INSTALL_DIR && $UV_CMD run --no-project python -m installation.provisioner --extra agent-browser"
            log_info "On apt-based systems, an admin also needs to run:"
            log_info "  sudo npx playwright install-deps chromium"
        else
        log_info "Setting up browser engine system libraries..."
        strip_snap_browser_override
        DETECTED_BROWSER_EXECUTABLE="$(find_system_browser 2>/dev/null || true)"
        if [ -n "$DETECTED_BROWSER_EXECUTABLE" ]; then
            log_success "Using explicit browser override: $DETECTED_BROWSER_EXECUTABLE"
            log_info "The system browser's libraries are already present (AGENT_BROWSER_EXECUTABLE_PATH is set)."
        else
            case "$DISTRO" in
                ubuntu|debian|raspbian|pop|linuxmint|elementary|zorin|kali|parrot)
                    # install-deps shells apt, so it needs root or
                    # passwordless sudo. Non-sudo users — typical for systemd
                    # service accounts and unprivileged operator users —
                    # would otherwise get blocked on an interactive sudo
                    # prompt that they can't satisfy; print the exact command
                    # the admin needs to run separately instead.
                    if [ "$(id -u)" -eq 0 ] || (command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null); then
                        log_info "Installing Chromium system dependencies (playwright install-deps)..."
                        cd "$INSTALL_DIR" && run_playwright_install 600 npx playwright install-deps chromium || {
                            log_warn "System-library install failed — browser tools may not work."
                            log_warn "Try running manually: sudo npx playwright install-deps chromium"
                        }
                    else
                        log_warn "No sudo available — skipping system-library install (install-deps)."
                        log_info "Ask an administrator to run, one time, as root:"
                        log_info "  sudo npx playwright install-deps chromium"
                        log_info "  (from $INSTALL_DIR, after Node.js deps are installed)"
                    fi
                    ;;
                arch|manjaro|cachyos|endeavouros|garuda)
                    if command -v pacman &> /dev/null; then
                        log_info "Arch-family distro detected — installing Chromium system dependencies via pacman..."
                        if command -v sudo &> /dev/null && sudo -n true 2>/dev/null; then
                            sudo NEEDRESTART_MODE=a pacman -S --noconfirm --needed \
                                nss atk at-spi2-core cups libdrm libxkbcommon mesa pango cairo alsa-lib >/dev/null 2>&1 || true
                        elif [ "$(id -u)" -eq 0 ]; then
                            pacman -S --noconfirm --needed \
                                nss atk at-spi2-core cups libdrm libxkbcommon mesa pango cairo alsa-lib >/dev/null 2>&1 || true
                        else
                            log_warn "Cannot install browser deps without sudo. Run manually:"
                            log_warn "  sudo pacman -S nss atk at-spi2-core cups libdrm libxkbcommon mesa pango cairo alsa-lib"
                        fi
                    fi
                    ;;
                fedora|rhel|centos|rocky|alma)
                    log_warn "Playwright does not support automatic dependency installation on RPM-based systems."
                    log_info "Install Chromium system dependencies manually before using browser tools:"
                    log_info "  sudo dnf install nss atk at-spi2-core cups-libs libdrm libxkbcommon mesa-libgbm pango cairo alsa-lib"
                    ;;
                opensuse*|sles)
                    log_warn "Playwright does not support automatic dependency installation on zypper-based systems."
                    log_info "Install Chromium system dependencies manually before using browser tools:"
                    log_info "  sudo zypper install mozilla-nss libatk-1_0-0 at-spi2-core cups-libs libdrm2 libxkbcommon0 Mesa-libgbm1 pango cairo libasound2"
                    ;;
                *)
                    log_warn "Playwright does not support automatic dependency installation on $DISTRO."
                    log_info "Install Chromium/browser system dependencies for your distribution before using browser tools."
                    ;;
            esac
        fi
        fi
        log_success "Browser engine setup complete"
    fi

    # Install TUI dependencies
    if [ -f "$INSTALL_DIR/ui-tui/package.json" ]; then
        log_info "Installing TUI dependencies..."
        cd "$INSTALL_DIR/ui-tui"
        # Time-boxed: a stalled registry fetch would otherwise hang here (#39219).
        # Report success only on actual success, same as node-deps above
        # (#77003) — and fail the install outright (#85297).
        # Capture npm output so failures are diagnosable (#87340).
        local tui_npm_log
        tui_npm_log="$(mktemp)"
        if ! run_with_timeout "$NODE_DEPS_TIMEOUT" npm install --silent \
                >"$tui_npm_log" 2>&1; then
            log_error "TUI npm install failed or timed out; TUI dependencies were not installed"
            if [ -s "$tui_npm_log" ]; then
                log_error "npm output:"
                cat "$tui_npm_log" >&2
            fi
            rm -f "$tui_npm_log"
            restore_dirty_lockfiles "$INSTALL_DIR"
            return 1
        fi
        rm -f "$tui_npm_log"
        log_success "TUI dependencies installed"
    fi

    # Keep the checkout clean so `hermes update` doesn't autostash every run.
    restore_dirty_lockfiles "$INSTALL_DIR"
}

install_browser_use_cli() {
    # The Browser Use CLI is the default browser backend when it is runnable
    # (tools/browser_use_cli.py). Provision it here so fresh installs don't
    # silently fall back to the built-in browser tools. Best-effort: any
    # failure is non-fatal because browser_exec can still run via uvx and
    # `hermes tools` can install it later.
    if [ "$SKIP_BROWSER" = true ]; then
        log_info "Skipping Browser Use CLI install (--skip-browser)"
        return 0
    fi
    if [ -z "$UV_CMD" ]; then
        log_info "Skipping Browser Use CLI install (uv unavailable)"
        return 0
    fi
    # MANAGED-FIRST: only Hermes' managed copy short-circuits. A browser-use
    # on the user's PATH is a side install — resolution prefers the managed
    # copy, so it must be provisioned regardless.
    if [ -x "$HERMES_HOME/bin/browser-use" ]; then
        log_success "Browser Use CLI already installed"
        return 0
    fi

    log_info "Installing Browser Use CLI (default browser backend)..."
    # UV_TOOL_BIN_DIR keeps the binary inside Hermes' managed bin dir, where
    # the browser tool resolves it — no reliance on the user's PATH.
    if run_with_timeout 600 env UV_NO_CONFIG=1 UV_TOOL_BIN_DIR="$HERMES_HOME/bin" \
        "$UV_CMD" tool install browser-use >/dev/null 2>&1; then
        log_success "Browser Use CLI installed"
    else
        log_warn "Browser Use CLI install failed — browser automation falls back to built-in tools."
        log_info "Install later with: $UV_CMD tool install browser-use  (or via 'hermes tools')"
    fi
}

run_setup_wizard() {
    if [ "$RUN_SETUP" = false ]; then
        log_info "Skipping setup wizard (--skip-setup)"
        return 0
    fi

    # The setup wizard reads from /dev/tty, so it works even when the
    # install script itself is piped (curl | bash). Only skip if no
    # terminal is available at all (e.g. Docker build, CI).
    #
    # Probe by actually opening /dev/tty: a bare existence test passes
    # in Docker builds where the device node is in the mount namespace
    # but opening fails with ENXIO, so the wizard would proceed and
    # then crash on `< /dev/tty` below.
    if ! (: </dev/tty) 2>/dev/null; then
        log_info "Setup wizard skipped (no terminal available). Run 'hermes setup' after install."
        return 0
    fi

    echo ""
    log_info "Starting setup wizard..."
    echo ""

    cd "$INSTALL_DIR"

    # Run hermes setup using the venv Python directly (no activation needed).
    # Redirect stdin from /dev/tty so interactive prompts work when piped from curl.
    if [ "$USE_VENV" = true ]; then
        "$INSTALL_DIR/venv/bin/python" -m hermes_cli.main setup < /dev/tty
    else
        python -m hermes_cli.main setup < /dev/tty
    fi
}

maybe_start_gateway() {
    # Check if any messaging platform tokens were configured
    ENV_FILE="$HERMES_HOME/.env"
    if [ ! -f "$ENV_FILE" ]; then
        return 0
    fi

    HAS_MESSAGING=false
    for VAR in TELEGRAM_BOT_TOKEN DISCORD_BOT_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN WHATSAPP_ENABLED; do
        VAL=$(grep "^${VAR}=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
        if [ -n "$VAL" ] && [ "$VAL" != "your-token-here" ]; then
            HAS_MESSAGING=true
            break
        fi
    done

    if [ "$HAS_MESSAGING" = false ]; then
        return 0
    fi

    echo ""
    log_info "Messaging platform token detected!"
    log_info "The gateway needs to be running for Hermes to send/receive messages."

    # If WhatsApp is enabled and no session exists yet, run foreground first for QR scan
    WHATSAPP_VAL=$(grep "^WHATSAPP_ENABLED=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
    WHATSAPP_SESSION="$HERMES_HOME/whatsapp/session/creds.json"
    if [ "$WHATSAPP_VAL" = "true" ] && [ ! -f "$WHATSAPP_SESSION" ]; then
        if [ "$IS_INTERACTIVE" = true ]; then
            echo ""
            log_info "WhatsApp is enabled but not yet paired."
            log_info "Running 'hermes whatsapp' to pair via QR code..."
            echo ""
            if prompt_yes_no "Pair WhatsApp now?" "yes"; then
                HERMES_CMD="$(get_hermes_command_path)"
                $HERMES_CMD whatsapp || true
            fi
        else
            log_info "WhatsApp pairing skipped (non-interactive). Run 'hermes whatsapp' to pair."
        fi
    fi

    # Probe by actually opening /dev/tty: a bare existence test passes
    # in Docker builds where the device node is in the mount namespace
    # but opening fails with ENXIO. See #16746.
    if ! (: </dev/tty) 2>/dev/null; then
        log_info "Gateway setup skipped (no terminal available). Run 'hermes gateway install' later."
        return 0
    fi

    echo ""
    local should_install_gateway=false
    if prompt_yes_no "Would you like to install the gateway as a background service?" "yes"; then
        should_install_gateway=true
    fi

    if [ "$should_install_gateway" = true ]; then
        HERMES_CMD="$(get_hermes_command_path)"

        if command -v systemctl &> /dev/null; then
            log_info "Installing systemd service..."
            if $HERMES_CMD gateway install 2>/dev/null; then
                log_success "Gateway service installed"
                if $HERMES_CMD gateway start 2>/dev/null; then
                    log_success "Gateway started! Your bot is now online."
                else
                    log_warn "Service installed but failed to start. Try: hermes gateway start"
                fi
            else
                log_warn "Systemd install failed. You can start manually: hermes gateway"
            fi
        else
            log_info "systemd not available — starting gateway in background..."
            nohup $HERMES_CMD gateway > "$HERMES_HOME/logs/gateway.log" 2>&1 &
            GATEWAY_PID=$!
            log_success "Gateway started (PID $GATEWAY_PID). Logs: ~/.hermes/logs/gateway.log"
            log_info "To stop: kill $GATEWAY_PID"
            log_info "To restart later: hermes gateway"
        fi
    else
        log_info "Skipped. Start the gateway later with: hermes gateway"
    fi
}

write_bootstrap_marker() {
    # Writes $INSTALL_DIR/.hermes-bootstrap-complete, which tells the Hermes
    # desktop app (apps/desktop/electron/main.ts) and the macOS launcher fast
    # path (apps/bootstrap-installer) "a real install finished here -- don't
    # re-run first-run bootstrap."
    #
    # Schema mirrors install.ps1's Write-BootstrapMarker and main.ts's
    # writeBootstrapMarker(). Keep the three in lockstep:
    #   schemaVersion 1 + pinnedCommit (length >= 7) are what the desktop
    #   validator requires; desktopVersion is omitted because only the desktop
    #   app knows its own version.
    if [ ! -d "$INSTALL_DIR" ]; then
        log_warn "Skipping bootstrap marker: $INSTALL_DIR doesn't exist"
        return 0
    fi

    # Explicit --commit wins; otherwise read HEAD from the checkout we just
    # installed. If neither resolves, skip the marker entirely rather than
    # write one the desktop will reject -- an absent marker is a clean
    # "bootstrap needed", a malformed one is a confusing half-state.
    local pinned_commit="$INSTALL_COMMIT"
    if [ -z "$pinned_commit" ]; then
        pinned_commit=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null) || pinned_commit=""
    fi

    if [ -z "$pinned_commit" ]; then
        log_warn "Skipping bootstrap marker: could not resolve HEAD in $INSTALL_DIR"
        return 0
    fi

    local marker_path="$INSTALL_DIR/.hermes-bootstrap-complete"
    local tmp_path="$marker_path.tmp"

    # Atomic publish: the macOS launcher predicate only checks existence, so a
    # torn write would arm the fast path against a half-written marker.
    printf '{\n  "schemaVersion": 1,\n  "pinnedCommit": "%s",\n  "pinnedBranch": "%s",\n  "completedAt": "%s"\n}\n' \
        "$pinned_commit" \
        "$BRANCH" \
        "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" > "$tmp_path"
    mv -f "$tmp_path" "$marker_path"
}

print_success() {
    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│              ✓ Installation Complete!                   │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
    echo ""

    # Show file locations
    echo -e "${CYAN}${BOLD}📁 Your files:${NC}"
    echo ""
    echo -e "   ${YELLOW}Config:${NC}    $HERMES_HOME/config.yaml"
    echo -e "   ${YELLOW}API Keys:${NC}  $HERMES_HOME/.env"
    echo -e "   ${YELLOW}Data:${NC}      $HERMES_HOME/cron/, sessions/, logs/"
    echo -e "   ${YELLOW}Code:${NC}      $INSTALL_DIR"
    echo ""

    echo -e "${CYAN}─────────────────────────────────────────────────────────${NC}"
    echo ""
    echo -e "${CYAN}${BOLD}🚀 Commands:${NC}"
    echo ""
    echo -e "   ${GREEN}hermes${NC}              Start chatting"
    echo -e "   ${GREEN}hermes setup${NC}        Configure API keys & settings"
    echo -e "   ${GREEN}hermes config${NC}       View/edit configuration"
    echo -e "   ${GREEN}hermes config edit${NC}  Open config in editor"
    echo -e "   ${GREEN}hermes gateway install${NC} Install gateway service (messaging + cron)"
    echo -e "   ${GREEN}hermes update${NC}       Update to latest version"
    echo ""

    echo -e "${CYAN}─────────────────────────────────────────────────────────${NC}"
    echo ""
    if [ "$ROOT_FHS_LAYOUT" = true ]; then
        echo -e "${YELLOW}⚡ 'hermes' was linked into /usr/local/bin and is ready to use — no shell reload needed.${NC}"
        echo ""
    else
        echo -e "${YELLOW}⚡ Reload your shell to use 'hermes' command:${NC}"
        echo ""
        LOGIN_SHELL="$(basename "${SHELL:-/bin/bash}")"
        if [ "$LOGIN_SHELL" = "zsh" ]; then
            echo "   source ~/.zshrc"
        elif [ "$LOGIN_SHELL" = "bash" ]; then
            echo "   source ~/.bashrc"
        elif [ "$LOGIN_SHELL" = "fish" ]; then
            echo "   source ~/.config/fish/config.fish"
        else
            echo "   source ~/.bashrc   # or ~/.zshrc"
        fi
        echo ""
    fi

}

# Clear the cached Electron download + any half-written unpacked output so the
# next `npm run pack` re-downloads and re-stages from scratch. A corrupt zip in
# the per-user Electron download cache - most often a partial/resumed download
# that leaves concatenated junk - makes electron-builder's `unpack-electron`
# extract a tree MISSING the electron binary, so the `electron`->`Hermes` rename
# dies with ENOENT and every re-run repeats the broken extraction forever. This
# is the bash sibling of install.ps1's Clear-ElectronBuildCache and the Python
# _purge_electron_build_cache() used by `hermes desktop`; install.sh was the only
# build path lacking it. Echoes the removed paths (one per line); best-effort.
clear_electron_build_cache() {
    local desktop_dir="$1"
    local removed=""

    # Per-user Electron download cache dirs, honoring the overrides @electron/get
    # respects, then the platform defaults (macOS: ~/Library/Caches/electron,
    # Linux: $XDG_CACHE_HOME/electron or ~/.cache/electron).
    local cache_dirs=()
    [ -n "${electron_config_cache:-}" ] && cache_dirs+=("$electron_config_cache")
    [ -n "${ELECTRON_CACHE:-}" ] && cache_dirs+=("$ELECTRON_CACHE")
    if [ "$OS" = "macos" ]; then
        cache_dirs+=("$HOME/Library/Caches/electron")
    else
        [ -n "${XDG_CACHE_HOME:-}" ] && cache_dirs+=("$XDG_CACHE_HOME/electron")
        cache_dirs+=("$HOME/.cache/electron")
    fi

    local dir zip
    for dir in "${cache_dirs[@]}"; do
        [ -d "$dir" ] || continue
        # Recurse: the bad copy may be the top-level zip OR a copy inside an
        # @electron/get hash subdir.
        while IFS= read -r zip; do
            [ -n "$zip" ] || continue
            if rm -f "$zip" 2>/dev/null; then
                removed="$removed$zip
"
            fi
        done <<EOF
$(find "$dir" -type f -name 'electron-*.zip' 2>/dev/null)
EOF
    done

    # A half-written unpacked dir from an interrupted prior pack poisons the
    # rename even after the zip is fixed (mac-arm64-unpacked / linux-unpacked).
    local release_dir="$desktop_dir/release"
    if [ -d "$release_dir" ]; then
        local unpacked
        while IFS= read -r unpacked; do
            [ -n "$unpacked" ] || continue
            if rm -rf "$unpacked" 2>/dev/null; then
                removed="$removed$unpacked
"
            fi
        done <<EOF
$(find "$release_dir" -maxdepth 1 -type d -name '*-unpacked' 2>/dev/null)
EOF
    fi

    printf '%s' "$removed"
}

# Run the desktop pack in $1 (the apps/desktop dir). `npm run pack` = tsc +
# vite build + electron-builder --dir, producing an unpacked app for the
# current OS. Signing auto-discovery is disabled so electron-builder falls back
# to an ad-hoc signature instead of grabbing an unrelated Developer ID from the
# keychain (a real signed/notarized .dmg needs Apple credentials — a separate
# release concern). Optional $2 = an ELECTRON_MIRROR base URL for this attempt,
# used as a fallback when the default GitHub release download is blocked.
_desktop_pack() {
    local desktop_dir="$1"
    local mirror="${2:-}"
    if [ -n "$mirror" ]; then
        ( cd "$desktop_dir" && ELECTRON_MIRROR="$mirror" CSC_IDENTITY_AUTO_DISCOVERY=false npm run pack )
    else
        ( cd "$desktop_dir" && CSC_IDENTITY_AUTO_DISCOVERY=false npm run pack )
    fi
}

# Last-resort Electron mirror after GitHub download fails (#47266).
DESKTOP_ELECTRON_FALLBACK_MIRROR="https://npmmirror.com/mirrors/electron/"

# Per-attempt wall-clock cap for the desktop npm install / electron-builder pack
# (#39219). A stalled (not failed) Electron download on a throttled/blocked link
# never returns, so without this the installer hangs forever on "Build desktop
# app". 900s is generous enough for a slow-but-progressing ~150MB fetch + build;
# override with DESKTOP_BUILD_TIMEOUT for very slow links.
DESKTOP_BUILD_TIMEOUT="${DESKTOP_BUILD_TIMEOUT:-900}"

# Wall-clock cap for the plain registry `npm install`s (browser-tools + TUI
# deps). Same #39219 stall class but no ~150MB Electron binary, so a shorter
# default; override with NODE_DEPS_TIMEOUT for very slow links.
NODE_DEPS_TIMEOUT="${NODE_DEPS_TIMEOUT:-600}"

# Electron package dir — workspace-local nest first, then root hoist.
_electron_dir() {
    local install_dir="$1"
    if [ -d "$install_dir/apps/desktop/node_modules/electron" ]; then
        printf '%s\n' "$install_dir/apps/desktop/node_modules/electron"
    else
        printf '%s\n' "$install_dir/node_modules/electron"
    fi
}

# True when dist/ holds a usable Electron binary (#38673 / run-electron-builder.mjs).
_electron_dist_ok() {
    local install_dir="$1"
    local electron_dir
    electron_dir="$(_electron_dir "$install_dir")"
    if [ "$OS" = "macos" ]; then
        [ -e "$electron_dir/dist/Electron.app/Contents/MacOS/Electron" ]
    else
        [ -e "$electron_dir/dist/electron" ]
    fi
}

# Best-effort: run electron/install.js to populate dist/ (optional mirror).
_restore_electron_dist() {
    local install_dir="$1"
    local mirror="${2:-}"
    local electron_dir
    electron_dir="$(_electron_dir "$install_dir")"
    _electron_dist_ok "$install_dir" && return 0

    [ -f "$electron_dir/install.js" ] || return 1
    command -v node >/dev/null 2>&1 || return 1

    rm -rf "$electron_dir/dist" 2>/dev/null || true
    rm -f "$electron_dir/path.txt" 2>/dev/null || true

    if [ -n "$mirror" ]; then
        ( cd "$electron_dir" && ELECTRON_MIRROR="$mirror" node install.js ) || true
    else
        ( cd "$electron_dir" && node install.js ) || true
    fi
    _electron_dist_ok "$install_dir"
}

_electron_pkg_staged_missing_dist() {
    local install_dir="$1"
    local electron_dir
    electron_dir="$(_electron_dir "$install_dir")"
    [ -f "$electron_dir/package.json" ] && [ -f "$electron_dir/install.js" ] && ! _electron_dist_ok "$install_dir"
}

_restore_electron_dist_with_fallback() {
    local install_dir="$1"
    _restore_electron_dist "$install_dir" \
        || { [ -z "${ELECTRON_MIRROR:-}" ] && _restore_electron_dist "$install_dir" "$DESKTOP_ELECTRON_FALLBACK_MIRROR"; }
}

# Build apps/desktop into a launchable native app. Mirrors install.ps1's
# Install-Desktop: a root-level npm install so the apps/* workspace resolves
# the desktop's own deps (Electron ~150MB), then `npm run pack`
# (electron-builder --dir) which emits an unpacked app for the current OS. Only invoked
# via the 'desktop' stage / --include-desktop, which the Electron app's own
# first-launch bootstrap never requests (it must not rebuild itself).
install_desktop_voice_deps() {
    # Desktop ships with working voice out of the box: eagerly install the
    # wake-word + local-STT stacks ([wake] + [voice] extras) instead of
    # leaving them to lazy first-use install. Policy change (Teknium, July
    # 2026, #70509 testing): the first ear-click used to trigger a
    # multi-minute onnxruntime pip install that froze the UI and blew RPC
    # timeouts. Lazy install remains the fallback for CLI-only installs and
    # for anything this best-effort step fails to fetch.
    local _prev_venv="${VIRTUAL_ENV:-}"
    if [ "$USE_VENV" = true ]; then
        export VIRTUAL_ENV="$INSTALL_DIR/venv"
    fi
    if [ -z "${UV_CMD:-}" ]; then
        install_uv || true
    fi
    if [ -z "${UV_CMD:-}" ]; then
        log_warn "uv unavailable — voice/wake deps will lazy-install at first use instead"
        return 0
    fi
    log_info "Installing voice + wake-word dependencies (onnxruntime, faster-whisper — 1-3min)..."
    if (cd "$INSTALL_DIR" && $UV_CMD pip install -e ".[wake,voice]") ; then
        log_success "Voice + wake-word dependencies installed"
    else
        log_warn "Voice/wake dependency install failed — they will lazy-install at first use"
    fi
    if [ "$USE_VENV" = true ] && [ -z "$_prev_venv" ]; then
        unset VIRTUAL_ENV
    fi
    return 0
}

install_desktop() {
    local desktop_dir="$INSTALL_DIR/apps/desktop"

    # The desktop stage only runs when a build is explicitly requested
    # (--include-desktop / 'desktop' stage), so a missing toolchain is a hard
    # failure, not a silent skip — a silent skip yields a "complete" install
    # with no app and a confusing "couldn't find a built desktop" at launch.
    # Re-provision here. Stages run in separate processes, so the PATH an
    # earlier stage exported is gone; this is a no-op when the pins are
    # already satisfied and it puts the managed tree back on PATH. The
    # build must never run on a system Node — Vite crashes on an old one,
    # which surfaced as the opaque "Build desktop app … exit code 1".
    provision_managed_runtimes || return
    if [ ! -f "$desktop_dir/package.json" ]; then
        log_warn "Skipping desktop build (apps/desktop not present in checkout)"
        return 0
    fi

    # 1. Root workspace install so apps/desktop's deps (Electron, Vite,
    #    node-pty prebuilds) resolve. The browser-tools install runs in the
    #    repo-root package workspace, which does not pull apps/* deps.
    #
    #    Prefer `npm ci`: it deletes node_modules and reinstalls from the
    #    lockfile, so it always produces a complete tree. Bare `npm install`
    #    can report "up to date" against a stale node_modules/.package-lock.json
    #    marker while node_modules is actually empty (Windows workspace-hoisting
    #    flake) — leaving tsc/typescript unresolved and `npm run pack`'s
    #    `tsc -b` failing with no obvious cause. Fall back to `npm install`
    #    only if `npm ci` is unavailable or the lockfile is out of sync.
    #
    #    Both the install and the build below are wrapped in a hard wall-clock
    #    timeout (#39219): the Electron binary (~150MB) is fetched from GitHub,
    #    and on a throttled/blocked connection that download can *stall* — npm
    #    neither errors nor exits, so the installer sits on "Build desktop app"
    #    forever with only `npm warn deprecated` lines visible. A stall now
    #    converts to a non-zero exit, which feeds the existing self-heal /
    #    mirror-fallback escalation instead of hanging the whole install.
    #
    #    The `npm ci` and its `npm install` fallback SHARE one budget: a stalled
    #    link wedges both identically, so giving each a full DESKTOP_BUILD_TIMEOUT
    #    would double the worst-case hang. We compute a single deadline and pass
    #    the remaining seconds to the fallback (min 30s so it still gets a real
    #    attempt if `npm ci` failed fast rather than stalling).
    log_info "Installing desktop workspace dependencies (includes Electron ~150MB, 1-3min)..."
    local _deps_start _deps_remaining
    _deps_start=$(date +%s)
    if run_with_timeout "$DESKTOP_BUILD_TIMEOUT" bash -c 'cd "$1" && npm ci' _ "$INSTALL_DIR"; then
        log_success "Desktop workspace dependencies installed"
    elif _deps_remaining=$(( DESKTOP_BUILD_TIMEOUT - ($(date +%s) - _deps_start) )); \
         [ "$_deps_remaining" -lt 30 ] && _deps_remaining=30; \
         run_with_timeout "$_deps_remaining" bash -c 'cd "$1" && npm install' _ "$INSTALL_DIR"; then
        log_success "Desktop workspace dependencies installed"
    elif _electron_pkg_staged_missing_dist "$INSTALL_DIR"; then
        log_warn "Desktop dependency install failed with a missing Electron dist; attempting self-heal..."
        _restore_electron_dist_with_fallback "$INSTALL_DIR" || true
    else
        log_error "Desktop workspace npm install failed"
        # Common cause: a previous 'sudo npm'/'sudo npx' left root-owned files in
        # ~/.npm, so this non-root install can't write the shared cache. npm hides
        # it behind a confusing EEXIST / "File exists" message while the real errno
        # is EACCES (-13). Point the user at the fix instead of a raw npm trace.
        log_info "If the errors above mention EACCES / 'permission denied' / EEXIST while"
        log_info "writing the npm cache, your ~/.npm likely holds root-owned files from an"
        log_info "earlier 'sudo npm' or 'sudo npx'. Reclaim ownership and retry:"
        log_info "  sudo chown -R \"\$(id -un)\" ~/.npm && npm cache verify"
        log_info "Then re-run this installer, or build manually:"
        log_info "  cd \"$INSTALL_DIR\" && npm ci && cd apps/desktop && npm run pack"
        return 1
    fi

    # 2. Build, with up to three escalating attempts so a transient/blocked
    #    Electron download self-heals instead of failing the whole install:
    #      a) plain `npm run pack` (downloads Electron from GitHub),
    #      b) on failure, purge a corrupt cached zip + stale unpacked dir and
    #         retry (matches install.ps1 / `hermes desktop`),
    #      c) on still-failing, fall back to a public Electron mirror — this is
    #         the GitHub-blocked/throttled case (the repeating "retrying" log).
    log_info "Building desktop app (this takes 1-3 minutes)..."
    local pack_ok=false
    if run_with_timeout "$DESKTOP_BUILD_TIMEOUT" _desktop_pack "$desktop_dir"; then
        pack_ok=true
    else
        local purged=""
        local restored=false
        if ! _electron_dist_ok "$INSTALL_DIR"; then
            purged="$(clear_electron_build_cache "$desktop_dir")"
            if _restore_electron_dist "$INSTALL_DIR"; then restored=true; fi
        fi
        if [ "$restored" = true ]; then
            log_warn "Desktop build failed; refreshed the Electron download and retrying once..."
            if run_with_timeout "$DESKTOP_BUILD_TIMEOUT" _desktop_pack "$desktop_dir"; then
                pack_ok=true
            fi
        fi
    fi

    # (c) GitHub blocked → mirror fallback (#47266).
    if [ "$pack_ok" = false ] && [ -z "${ELECTRON_MIRROR:-}" ]; then
        log_warn "Desktop build still failing — the Electron download from GitHub looks blocked."
        log_warn "Re-downloading Electron via a public mirror ($DESKTOP_ELECTRON_FALLBACK_MIRROR), then rebuilding..."
        log_warn "  (set ELECTRON_MIRROR yourself to use a different/trusted mirror)"
        _electron_dist_ok "$INSTALL_DIR" || _restore_electron_dist "$INSTALL_DIR" "$DESKTOP_ELECTRON_FALLBACK_MIRROR" || true
        if run_with_timeout "$DESKTOP_BUILD_TIMEOUT" _desktop_pack "$desktop_dir" "$DESKTOP_ELECTRON_FALLBACK_MIRROR"; then
            pack_ok=true
        fi
    fi

    if [ "$pack_ok" = false ]; then
        log_error "Desktop app build failed"
        # If the log shows repeated "retrying" lines fetching the Electron zip,
        # the binary download is blocked/throttled (firewall, proxy, region) and
        # the mirror fallback above also couldn't reach a host. Try a mirror you
        # trust and rebuild (@electron/get honors ELECTRON_MIRROR):
        log_info "If the log shows Electron download retries, rebuild via a reachable mirror:"
        log_info "  ELECTRON_MIRROR=<mirror-base-url> \\"
        log_info "    bash -c 'cd \"$desktop_dir\" && CSC_IDENTITY_AUTO_DISCOVERY=false npm run pack'"
        log_info "Otherwise build manually: cd $desktop_dir && npm run pack"
        return 1
    fi

    local app=""
    if [ "$OS" = "linux" ]; then
        if [ -x "$desktop_dir/release/linux-unpacked/Hermes" ]; then
            app="$desktop_dir/release/linux-unpacked/Hermes"
        elif [ -x "$desktop_dir/release/linux-unpacked/hermes" ]; then
            app="$desktop_dir/release/linux-unpacked/hermes"
        fi
    else
        local cand
        for cand in \
            "$desktop_dir/release/mac-arm64/Hermes.app" \
            "$desktop_dir/release/mac/Hermes.app"; do
            if [ -d "$cand" ]; then
                app="$cand"
                break
            fi
        done
    fi
    if [ -z "$app" ]; then
        log_error "Desktop build completed but no app was found under $desktop_dir/release/"
        return 1
    fi
    log_success "Desktop app built: $app"

    # Linux: Electron's chrome-sandbox helper needs root:root 4755 or the
    # sandboxed renderer will abort on startup.  Check the file is a regular
    # file (not a symlink) before chown/chmod so we don't follow an
    # attacker-controlled link to an arbitrary path.
    if [ "$OS" = "linux" ]; then
        local sandbox="$desktop_dir/release/linux-unpacked/chrome-sandbox"
        if [ -f "$sandbox" ] && [ ! -L "$sandbox" ]; then
            if [ "$(id -u)" -eq 0 ]; then
                chown root:root "$sandbox" && chmod 4755 "$sandbox" || {
                    log_error "Cannot configure Electron sandbox helper: $sandbox"
                    return 1
                }
            elif command -v sudo >/dev/null 2>&1; then
                sudo chown root:root "$sandbox" && sudo chmod 4755 "$sandbox" || {
                    log_error "Cannot configure Electron sandbox helper (sudo failed): $sandbox"
                    return 1
                }
            else
                log_error "Cannot configure Electron sandbox helper without sudo: $sandbox"
                return 1
            fi
        fi
    fi

    # macOS: route through the same config-aware signing fixup as
    # `hermes desktop`, so install/repair and self-update agree about the app's
    # identity. The fixup preserves the Electron entitlement plists and signs
    # with a stable Designated Requirement (configured keychain identity, else
    # identifier-pinned ad-hoc), so macOS TCC grants — Full Disk Access,
    # Desktop/Downloads/Documents, Accessibility, microphone — survive the
    # rebuild instead of resetting on every update. The shell's
    # publisher-signing decision governed the build and is passed explicitly so
    # importing Python cannot reverse it by loading HERMES_HOME/.env. If the
    # helper is unavailable or fails, branch into the historical quarantine
    # strip + deep ad-hoc repair so a broken venv never leaves the bundle
    # unsigned/unlaunchable.
    if [ "$OS" = "macos" ] && [ -z "${CSC_LINK:-}" ] && [ -z "${APPLE_SIGNING_IDENTITY:-}" ] && command -v codesign >/dev/null 2>&1; then
        local config_python="$INSTALL_DIR/venv/bin/python"
        local fixup_ok=""
        if [ -x "$config_python" ]; then
            if HERMES_HOME="$HERMES_HOME" "$config_python" - "$desktop_dir" <<'PYEOF'
import sys
from pathlib import Path
from hermes_cli.main import _desktop_macos_relaunchable_fixup
ok = _desktop_macos_relaunchable_fixup(
    Path(sys.argv[1]), publisher_signing_configured=False
)
sys.exit(0 if ok else 1)
PYEOF
            then
                fixup_ok=1
            else
                log_warn "Config-aware macOS signing fixup failed; applying the historical ad-hoc fallback."
            fi
        fi
        if [ -z "$fixup_ok" ]; then
            xattr -cr "$app" 2>/dev/null || true
            codesign --force --deep --sign - "$app" >/dev/null 2>&1 || true
        fi
    fi

    # `npm install` + `npm run pack` rewrite lockfiles; restore them so the
    # checkout stays clean for the next `hermes update`.
    restore_dirty_lockfiles "$INSTALL_DIR"
}

# Each --stage runs in its own process, so (unlike the monolithic main() where
# clone_repo cd's once and later steps inherit it) a stage that operates on the
# checkout must cd into it explicitly. Without this, install_deps/setup_path run
# from the desktop app's cwd and resolve `.` / the venv against the wrong tree.
require_install_dir() {
    if [ -z "$INSTALL_DIR" ] || [ ! -d "$INSTALL_DIR" ]; then
        log_error "Install directory not found: ${INSTALL_DIR:-<unset>}"
        log_info "The 'repository' stage must run before this one."
        return 1
    fi
    cd "$INSTALL_DIR"
}

# Desktop bootstrap stage protocol. Mirrors the Windows install.ps1 surface
# closely enough for the Electron bootstrap runner to show structured progress.
run_stage_body() {
    local stage="$1"

    case "$stage" in
        prerequisites)
            print_banner
            detect_os
            resolve_install_layout
            # uv + python move to the venv stage: the pinned uv lands in
            # the install-scoped runtime dir, which exists only after the
            # repository stage clones the checkout.
            check_git
            check_network_prerequisites
            install_system_packages
            ;;
        repository)
            detect_os
            resolve_install_layout
            check_git
            clone_repo
            ;;
        venv)
            detect_os
            resolve_install_layout
            require_install_dir
            install_uv
            check_python
            setup_venv
            ;;
        python-deps)
            detect_os
            resolve_install_layout
            require_install_dir
            install_uv
            check_python
            install_deps
            ;;
        node-deps)
            detect_os
            resolve_install_layout
            require_install_dir
            # Stage NAME is protocol (the GUI driver renders it); its body is
            # now the shared provisioner. Managed runtimes first, then the
            # npm-installed browser tooling that needs a working node.
            provision_managed_runtimes || return
            install_node_deps
            ;;
        path)
            detect_os
            resolve_install_layout
            require_install_dir
            setup_path
            ;;
        config)
            detect_os
            resolve_install_layout
            require_install_dir
            copy_config_templates
            ;;
        setup)
            detect_os
            resolve_install_layout
            require_install_dir
            run_setup_wizard
            ;;
        gateway)
            detect_os
            resolve_install_layout
            require_install_dir
            maybe_start_gateway
            ;;
        desktop)
            detect_os
            resolve_install_layout
            require_install_dir
            # Each stage runs in its own process, so the managed Node
            # provisioned during node-deps (in the install root's
            # .hermes-runtime) isn't on PATH here. Re-provisioning is a
            # cheap no-op when the pin is already satisfied, and it puts
            # the tree back on PATH so install_desktop finds npm.
            provision_managed_runtimes || return
            install_desktop_voice_deps
            install_desktop
            ;;
        complete)
            detect_os
            resolve_install_layout
            print_success
            write_bootstrap_marker
            ;;
        *)
            log_error "Unknown stage: $stage"
            return 2
            ;;
    esac
}

run_stage_protocol() {
    local stage="$1"
    if [ -z "$stage" ]; then
        log_error "--stage requires a stage name"
        if [ "$JSON_OUTPUT" = true ]; then
            emit_stage_json "" false false "missing stage name"
        fi
        return 2
    fi

    if [ "$NON_INTERACTIVE" = true ] && stage_needs_user_input "$stage"; then
        log_info "Skipping $stage (non-interactive bootstrap)"
        if [ "$JSON_OUTPUT" = true ]; then
            emit_stage_json "$stage" true true
        fi
        return 0
    fi

    # Run the stage body in a subshell so a stage helper that calls `exit 1`
    # on failure (clone_repo, install_deps, etc. were written for the monolithic
    # flow) only exits the subshell — the parent still reaches the JSON result
    # frame below. Without this, a failed --stage would terminate the process
    # before emitting the frame and the Rust/Electron parser would see "no
    # result frame" instead of a clean {ok:false} contract response.
    set +e
    ( run_stage_body "$stage" )
    local code=$?
    set -e

    if [ "$JSON_OUTPUT" = true ]; then
        if [ "$code" -eq 0 ]; then
            emit_stage_json "$stage" true false
        else
            emit_stage_json "$stage" false false "exit code $code"
        fi
    fi
    return "$code"
}

# ============================================================================
# Main
# ============================================================================

main() {
    print_banner

    detect_os
    resolve_install_layout
    check_git
    check_network_prerequisites
    install_system_packages

    clone_repo
    # AFTER the clone: the pinned uv lives in the install-scoped runtime
    # dir ($INSTALL_DIR/.hermes-runtime/uv/), which exists once the
    # checkout does. git is the only true pre-clone prerequisite.
    install_uv
    check_python
    # Before the venv: the provisioner is stdlib-only and runs under
    # `uv run --no-project`, and the tools it installs (node, npm, git) are
    # what the steps below build with.
    provision_managed_runtimes || return
    setup_venv
    install_deps
    # The hermes command exists as soon as install_deps finishes — link it
    # NOW. When a later optional stage fails (browser deps needing npm, the
    # cua driver download), the user still gets a working `hermes` plus a
    # clear warning, instead of a complete install whose only symptom is
    # "command not found" because the symlink stage was never reached.
    setup_path
    install_node_deps || return
    install_browser_use_cli
    copy_config_templates
    run_setup_wizard
    maybe_start_gateway

    if [ "$INCLUDE_DESKTOP" = true ]; then
        install_desktop_voice_deps
        install_desktop
    fi

    print_success

    write_bootstrap_marker
}

if [ "$MANIFEST_MODE" = true ]; then
    emit_manifest
elif [ -n "$STAGE_NAME" ]; then
    run_stage_protocol "$STAGE_NAME"
else
    main
fi
