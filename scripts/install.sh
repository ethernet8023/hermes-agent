#!/usr/bin/env bash
# Hermes Agent bootstrap: clone, acquire uv/Python, then hand the checkout to
# the same completion an update runs -- command publication, product builds and
# post-build maintenance -- so a fresh install and a finished update land in one
# state. Heavy dependencies (tool binaries, browsers, node) are pm's job:
# `hermes pm install`.
#
# Stage protocol kept for Hermes-Setup:
#   --manifest            print the stage list as JSON
#   --stage NAME [--json] run one stage
#   --non-interactive     skip stages that need input
#   --include-desktop     build the desktop app too (products stage)
set -u

# Prevent uv from discovering config files (uv.toml, pyproject.toml) from the
# wrong user's home directory when running under sudo -u <user>.  See #21269.
# pm's own venv sync re-isolates (pm/environment.py), so this bootstrap
# hygiene can't break the locked sync the way it used to before pm owned it.
export UV_NO_CONFIG=1

REPO_URL="${HERMES_REPO_URL:-https://github.com/NousResearch/hermes-agent.git}"
BRANCH="main"
INSTALL_COMMIT=""
INSTALL_DIR="${HERMES_INSTALL_DIR:-}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
STAGE=""
WANT_MANIFEST=false
JSON=false
NON_INTERACTIVE=false
INCLUDE_DESKTOP=false

while [ $# -gt 0 ]; do
    case "$1" in
        --branch|-Branch|--commit|-Commit|--dir|--hermes-home|-HermesHome|--stage|-Stage)
            option="$1"
            if [ $# -lt 2 ] || [ -z "$2" ] || [[ "$2" == -* ]]; then
                printf '%s needs a value\n' "$option" >&2
                exit 2
            fi
            case "$option" in
                --branch|-Branch) BRANCH="$2" ;;
                --commit|-Commit) INSTALL_COMMIT="$2" ;;
                --dir) INSTALL_DIR="$2" ;;
                --hermes-home|-HermesHome) HERMES_HOME="$2" ;;
                --stage|-Stage) STAGE="$2" ;;
            esac
            shift 2 ;;
        --manifest|-Manifest) WANT_MANIFEST=true; shift ;;
        --json|-Json) JSON=true; shift ;;
        --non-interactive|-NonInteractive) NON_INTERACTIVE=true; shift ;;
        --skip-setup) NON_INTERACTIVE=true; shift ;;
        --skip-browser|--no-playwright)
            echo "$1 no longer skips the browser install; pm manages browser dependencies. Remove this flag and use --non-interactive to skip setup prompts." >&2
            exit 1 ;;
        --include-desktop|-IncludeDesktop) INCLUDE_DESKTOP=true; shift ;;
        -h|--help)
            echo "Usage: install.sh [--branch NAME] [--commit SHA] [--dir PATH]"
            echo "                  [--hermes-home PATH]"
            echo "                  [--manifest] [--stage NAME] [--json]"
            echo "                  [--non-interactive] [--include-desktop]"
            exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

INSTALL_DIR="${INSTALL_DIR:-$HERMES_HOME/hermes-agent}"
export HERMES_HOME

log() { printf "\033[1;34m[hermes]\033[0m %s\n" "$1"; }
fail() { STAGE_REASON="$1"; printf "\033[1;31m[hermes]\033[0m %s\n" "$1" >&2; exit 1; }

# --- BEGIN GENERATED: bootstrap pins (scripts/gen-bootstrap-pins.py) ---
# Derived from pm/lock.json. DO NOT EDIT BY HAND:
# run scripts/gen-bootstrap-pins.py after a pin bump.
UV_PIN_VERSION="0.12.3"

# Sets UV_PIN_URL + UV_PIN_SHA256 for a <os>-<arch> target key.
uv_bootstrap_pin() {
    case "$1" in
        linux-x64)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-x86_64-unknown-linux-gnu.tar.gz"
            UV_PIN_MIRROR="https://hermes-assets.nousresearch.com/upstream/sha256/600cf9a742aca00d292673b16b5acffaa7b8c269a364ad0c2e79498dcb1fe101"
            UV_PIN_SHA256="600cf9a742aca00d292673b16b5acffaa7b8c269a364ad0c2e79498dcb1fe101"
            ;;
        linux-arm64)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-aarch64-unknown-linux-gnu.tar.gz"
            UV_PIN_MIRROR="https://hermes-assets.nousresearch.com/upstream/sha256/bb66cb52e7b1823aed1183630d8d8e5c958840d584a4c55ec10a4cfc168dcca2"
            UV_PIN_SHA256="bb66cb52e7b1823aed1183630d8d8e5c958840d584a4c55ec10a4cfc168dcca2"
            ;;
        darwin-x64)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-x86_64-apple-darwin.tar.gz"
            UV_PIN_MIRROR="https://hermes-assets.nousresearch.com/upstream/sha256/4c9f52262a14da336e4a42ed24992d12d0c956acde87619e4611d321dffa602b"
            UV_PIN_SHA256="4c9f52262a14da336e4a42ed24992d12d0c956acde87619e4611d321dffa602b"
            ;;
        darwin-arm64)
            UV_PIN_URL="https://github.com/astral-sh/uv/releases/download/0.12.3/uv-aarch64-apple-darwin.tar.gz"
            UV_PIN_MIRROR="https://hermes-assets.nousresearch.com/upstream/sha256/546f7f8a6c70ff13a3a9d2bc958db3427298cebf3e0cb756f9177133b7068843"
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

uv_bootstrap_target() {
    # Map this host to a pm/lock.json target key (<os>-<arch>).
    local _arch
    case "$(uname -m)" in
        arm64|aarch64) _arch="arm64" ;;
        x86_64|amd64)  _arch="x64" ;;
        *) return 1 ;;
    esac
    case "$(uname -s)" in
        Linux)  echo "linux-$_arch" ;;
        Darwin) echo "darwin-$_arch" ;;
        *) return 1 ;;
    esac
}

# version_at_least HAVE WANT: dotted numeric comparison; a pre-release or
# build suffix on a component is ignored ("0.12.3-rc1" reads as 0.12.3).
version_at_least() {
    local have="$1" want="$2" h w
    while [ -n "$want" ]; do
        h="${have%%.*}"; h="${h%%[!0-9]*}"
        w="${want%%.*}"; w="${w%%[!0-9]*}"
        [ "${h:-0}" -gt "${w:-0}" ] && return 0
        [ "${h:-0}" -lt "${w:-0}" ] && return 1
        case "$have" in *.*) have="${have#*.}" ;; *) have="" ;; esac
        case "$want" in *.*) want="${want#*.}" ;; *) want="" ;; esac
    done
    return 0
}

# Provision uv for this host from the pinned pm/lock.json artifact. Stages
# the EXACT artifact pm itself uses into the same store slot
# (<store>/uv-<version>-<target>/, the store pm's store_root() resolves),
# sha256-verified, so the byte authority is pm/lock.json - no astral-latest,
# no curl|sh.
UV_CMD=""
ensure_uv() {
    [ -n "$UV_CMD" ] && return 0
    local _path_uv _path_version
    if _path_uv="$(command -v uv 2>/dev/null)"; then
        # Developer shortcut: a uv on PATH fetches nothing, but only one at
        # least as new as the pin -- the bootstrap passes flags older uv
        # lacks (`python install --no-bin` arrived in 0.7).
        _path_version="$("$_path_uv" --version 2>/dev/null | awk '{print $2}')"
        if [ -n "$_path_version" ] && version_at_least "$_path_version" "$UV_PIN_VERSION"; then
            UV_CMD="$_path_uv"
            return 0
        fi
        log "uv on PATH (${_path_version:-does not run}) is older than the pinned $UV_PIN_VERSION; staging the pin"
    fi
    local _target
    if ! _target="$(uv_bootstrap_target)"; then
        fail "no pinned uv build for this platform ($(uname -s) $(uname -m)); install uv manually: https://docs.astral.sh/uv/"
    fi
    if ! uv_bootstrap_pin "$_target"; then
        fail "no pinned uv artifact for $_target; install uv manually: https://docs.astral.sh/uv/"
    fi
    local _store="${HERMES_RUNTIME_DIR:-$HERMES_HOME/tools}"
    local _entry="$_store/uv-$UV_PIN_VERSION-$_target"
    UV_CMD="$_entry/uv"
    if [ ! -x "$UV_CMD" ]; then
        log "staging pinned uv $UV_PIN_VERSION ($_target) into the pm store"
        local _tmp
        # no-tmp: ok — last-resort fallback when mktemp itself is missing
        _tmp="$(mktemp -d 2>/dev/null || echo "/tmp/hermes-uv-bootstrap.$$")"
        mkdir -p "$_tmp"
        local _fetched_from="$UV_PIN_URL"
        # Only network availability failures permit trying identical mirrored bytes.
        if curl -LsSf "$UV_PIN_URL" -o "$_tmp/uv.tar.gz"; then
            :
        else
            local _curl_status=$?
            case "$_curl_status" in
                5|6|7|18|22|28|52|55|56) ;;
                *) rm -rf "$_tmp"; fail "failed to download pinned uv from $UV_PIN_URL (curl $_curl_status)" ;;
            esac
            if [ -n "${UV_PIN_MIRROR:-}" ] && curl -LsSf "$UV_PIN_MIRROR" -o "$_tmp/uv.tar.gz"; then
                _fetched_from="$UV_PIN_MIRROR"
            else
                rm -rf "$_tmp"
                fail "failed to download pinned uv from $UV_PIN_URL or ${UV_PIN_MIRROR:-no mirror}"
            fi
        fi
        local _digest
        if command -v sha256sum >/dev/null 2>&1; then
            _digest="$(sha256sum "$_tmp/uv.tar.gz" | cut -d' ' -f1)"
        else
            _digest="$(shasum -a 256 "$_tmp/uv.tar.gz" | cut -d' ' -f1)"
        fi
        if [ "$_digest" != "$UV_PIN_SHA256" ]; then
            rm -rf "$_tmp"
            fail "uv download digest mismatch from $_fetched_from (expected $UV_PIN_SHA256, got $_digest)"
        fi
        if ! tar -xzf "$_tmp/uv.tar.gz" -C "$_tmp"; then
            rm -rf "$_tmp"
            fail "failed to extract pinned uv archive"
        fi
        local _unpacked
        _unpacked="$(find "$_tmp" -mindepth 1 -maxdepth 2 -name uv -type f | head -n1)"
        if [ -z "$_unpacked" ]; then
            rm -rf "$_tmp"
            fail "uv binary not found in the downloaded archive"
        fi
        mkdir -p "$_entry"
        mv "$_unpacked" "$UV_CMD"
        [ -f "$(dirname "$_unpacked")/uvx" ] && mv "$(dirname "$_unpacked")/uvx" "$_entry/uvx"
        chmod +x "$UV_CMD"
        chmod +x "$_entry/uvx" 2>/dev/null || true
        rm -rf "$_tmp"
    fi
    # Bootstrap keeps the installer private; only UV_CMD invokes it.
    if ! "$UV_CMD" --version >/dev/null 2>&1; then
        fail "pinned uv staged but does not run on this host"
    fi
    log "uv ready ($("$UV_CMD" --version 2>/dev/null))"
}

check_platform() {
    # Termux is Linux by uname, but this installer builds a glibc source
    # install the phone cannot run (no Android wheels in the lock). The
    # signed APT package is the only supported shape there.
    if [ -n "${TERMUX_VERSION:-}" ] || case "${PREFIX:-}" in *com.termux/files/usr*) true ;; *) false ;; esac; then
        fail "Termux is installed from its APT repository, not install.sh: pkg install hermes-agent (setup: https://hermes-agent.nousresearch.com/docs/getting-started/termux)"
    fi
    case "$(uname -s 2>/dev/null)" in
        Linux*) : ;;
        Darwin*) : ;;
        *) fail "unsupported platform: $(uname -s). On Windows use install.ps1." ;;
    esac
}

json_string() {
    local value="$1" code char escaped
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    for ((code = 1; code < 32; code++)); do
        printf -v char '\\%03o' "$code"
        printf -v char '%b' "$char"
        printf -v escaped '\\u%04x' "$code"
        value="${value//"$char"/$escaped}"
    done
    printf '"%s"' "$value"
}

json_frame() {
    # $1 ok, $2 stage, $3 skipped, $4 reason
    if [ -n "${4:-}" ]; then
        printf '{"ok":%s,"stage":%s,"skipped":%s,"reason":%s}\n' "$1" "$(json_string "$2")" "$3" "$(json_string "$4")"
    else
        printf '{"ok":%s,"stage":%s,"skipped":%s}\n' "$1" "$(json_string "$2")" "$3"
    fi
}

stage_result() {
    local code="$1" ok=false reason="${STAGE_REASON:-}"
    if [ "$code" -eq 0 ]; then
        ok=true
    else
        reason="${reason:-stage failed (exit $code)}"
    fi
    if [ "$JSON" = true ]; then
        json_frame "$ok" "$STAGE" "${STAGE_SKIPPED:-false}" "$reason"
    fi
}

# The single authoritative stage list: emit_manifest prints it AND the
# no-flag ladder runs it. `products` is the shared completion tail -- the same
# call `hermes update` makes -- so the manifest and the run cannot disagree.
# `desktop` stays directly dispatchable via --stage for external callers, but
# is never listed: --include-desktop selects the desktop product inside
# `products` instead of adding a second build stage.
stage_names() {
    printf '%s\n' prerequisites repository venv python-deps config products setup gateway complete
}

# "title|category|needs_user_input".
products_record() {
    if [ "$INCLUDE_DESKTOP" = true ]; then
        echo "Install command and app + desktop|runtime|false"
    else
        echo "Install command and app|runtime|false"
    fi
}

# "$1" stage name -> its manifest record fields (title|category|needs_user_input).
stage_record() {
    case "$1" in
        prerequisites) echo "System prerequisites|runtime|false" ;;
        repository)    echo "Download Hermes Agent|runtime|false" ;;
        venv)          echo "Create Python environment|runtime|false" ;;
        python-deps)   echo "Install Python dependencies|runtime|false" ;;
        config)        echo "Prepare config and skills|configuration|false" ;;
        products)      products_record ;;
        setup)         echo "Configure API keys and settings|configuration|true" ;;
        gateway)       echo "Configure gateway service|configuration|true" ;;
        desktop)       echo "Build desktop app|runtime|false" ;;
        complete)      echo "Finish install|runtime|false" ;;
    esac
}

emit_manifest() {
    printf '%s' '{"protocol_version":1,"stages":['
    _sep=""
    for _s in $(stage_names); do
        IFS='|' read -r _title _category _needs <<< "$(stage_record "$_s")"
        printf '%s{"name":"%s","title":"%s","category":"%s","needs_user_input":%s}' \
            "$_sep" "$_s" "$_title" "$_category" "$_needs"
        _sep=","
    done
    printf '%s\n' ']}'
}

stage_prerequisites() {
    command -v git >/dev/null 2>&1 || fail "git is required. Install it with your system package manager."
    command -v curl >/dev/null 2>&1 || fail "curl is required. Install it with your system package manager."
    log "prerequisites ok (git, curl)"
}

stage_repository() {
    # An interrupted clone from an older installer can leave a .git with no
    # initial commit, where stash/checkout abort ("You do not have the
    # initial commit yet", #40998). Move it aside -- never delete it, it may
    # hold something the user wants -- and clone fresh below.
    if [ -d "$INSTALL_DIR/.git" ] && ! git -C "$INSTALL_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
        local broken
        broken="${INSTALL_DIR}.broken-$(date -u +%Y%m%d-%H%M%S)"
        log "$INSTALL_DIR has no commits (interrupted clone); moving it aside to $broken"
        mv "$INSTALL_DIR" "$broken" || fail "cannot move $INSTALL_DIR aside"
    fi
    if [ -d "$INSTALL_DIR/.git" ]; then
        log "updating $INSTALL_DIR"
        # An explicit HERMES_REPO_URL names the source for reruns too, not
        # just the first clone.
        if [ -n "${HERMES_REPO_URL:-}" ]; then
            git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL" || fail "cannot point origin at $REPO_URL"
        fi
        git -C "$INSTALL_DIR" fetch origin "$BRANCH" || fail "git fetch failed"
        local stamp
        stamp="$(date -u +%Y%m%d-%H%M%S)"
        # Park local work BEFORE switching branches: checkout refuses a dirty
        # tree that conflicts, and the reset below would discard it. Work
        # that cannot be parked stops the install -- never overwrite it.
        if [ -n "$(git -C "$INSTALL_DIR" status --porcelain)" ]; then
            # An interrupted update can leave unmerged index entries, where
            # stash aborts ("could not write index"). Dropping only the
            # index-level conflict state keeps the working-tree changes for
            # the stash below (#4735).
            if [ -n "$(git -C "$INSTALL_DIR" ls-files --unmerged)" ]; then
                log "clearing unmerged index entries from a previous conflict"
                git -C "$INSTALL_DIR" reset -q || fail "cannot clear the unmerged index in $INSTALL_DIR"
            fi
            git -C "$INSTALL_DIR" stash push --include-untracked -m "hermes-install-autostash-$stamp" \
                || fail "could not stash local changes in $INSTALL_DIR; commit or move them aside, then rerun"
            log "local changes stashed as hermes-install-autostash-$stamp"
        fi
        git -C "$INSTALL_DIR" checkout "$BRANCH" || fail "git checkout failed"
        if ! git -C "$INSTALL_DIR" merge --ff-only "origin/$BRANCH"; then
            # A release cut off the main line, a force-pushed remote, or the
            # user's own commits cannot fast-forward. Every stage below reads
            # files only the new tree has (pm/), so an install left on the old
            # tree cannot finish -- match the remote the way `hermes update`
            # does, after parking the old tip.
            # Only commits absent from origin need a rescue ref. Keep the same
            # namespace as `hermes update` so its pruning and recovery work.
            local dropped rescue_kind rescue_ref prior
            dropped="$(git -C "$INSTALL_DIR" rev-list --count "origin/$BRANCH..HEAD")" \
                || fail "cannot count commits before reset"
            if [ "$dropped" -gt 0 ]; then
                rescue_kind="diverged"
                git -C "$INSTALL_DIR" merge-base HEAD "origin/$BRANCH" >/dev/null 2>&1 \
                    || rescue_kind="orphan"
                prior="$(git -C "$INSTALL_DIR" rev-parse --short=12 HEAD)" \
                    || fail "cannot identify commits before reset"
                rescue_ref="refs/hermes-update-backups/$rescue_kind-$BRANCH-$stamp-$prior"
                git -C "$INSTALL_DIR" update-ref "$rescue_ref" HEAD \
                    || fail "cannot back up $dropped local commit(s); refusing to reset"
                log "$dropped commit(s) not on origin/$BRANCH backed up to $rescue_ref"
                log "List them with: git -C \"$INSTALL_DIR\" log origin/$BRANCH..$rescue_ref"
            fi
            git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH" || fail "git reset failed"
            log "not fast-forwardable; reset to origin/$BRANCH"
        fi
    else
        # `mv <clone> <existing dir>` nests the checkout INSIDE it as
        # <dir>/tree, so a pre-existing destination must be empty (we take
        # the empty dir over) or we refuse: whatever lives there is not ours.
        if [ -e "$INSTALL_DIR" ] || [ -L "$INSTALL_DIR" ]; then
            if [ -d "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ] && [ -z "$(ls -A "$INSTALL_DIR")" ]; then
                rmdir "$INSTALL_DIR" || fail "cannot replace empty $INSTALL_DIR"
            else
                fail "$INSTALL_DIR exists and is not a Hermes git checkout. Move it aside, or install elsewhere with --dir <path>."
            fi
        fi
        mkdir -p "$(dirname "$INSTALL_DIR")"
        local staged attempt cloned=false
        staged="$(mktemp -d "$(dirname "$INSTALL_DIR")/.hermes-clone-XXXXXX")" || fail "cannot stage clone"
        log "cloning $REPO_URL ($BRANCH) into $INSTALL_DIR (staged at $staged/tree)"
        for attempt in 1 2 3; do
            # Treeless: every commit and release tag (runtime identity is the
            # nearest reachable release; --commit pins and branch switches
            # still resolve), trees and blobs fetched on demand, so the
            # download stays close to a --depth 1 clone.
            if git clone --filter=tree:0 --branch "$BRANCH" "$REPO_URL" "$staged/tree"; then
                cloned=true
                break
            fi
            rm -rf "$staged/tree"
            [ "$attempt" = 3 ] || sleep "$((attempt * 5))"
        done
        if [ "$cloned" = false ]; then
            # The checkout step is where throttled downloads die: clone the
            # graph alone, then retry materializing the tree separately.
            log "direct clone failed; trying deferred checkout"
            if git clone --filter=tree:0 --no-checkout \
                --branch "$BRANCH" "$REPO_URL" "$staged/tree"; then
                for attempt in 1 2; do
                    if git -C "$staged/tree" reset --hard HEAD; then
                        cloned=true
                        break
                    fi
                    [ "$attempt" = 2 ] || sleep 5
                done
            fi
        fi
        if [ "$cloned" = false ]; then
            rm -rf "$staged"
            fail "git clone failed; no checkout published"
        fi
        if ! mv "$staged/tree" "$INSTALL_DIR"; then
            rm -rf "$staged"
            fail "cannot publish cloned checkout"
        fi
        rmdir "$staged"
    fi
    if [ -n "$INSTALL_COMMIT" ]; then
        # A pin must come from the branch being installed: the complete
        # marker records both, and a commit off that branch would make the
        # next plain rerun "update" onto a different line.
        git -C "$INSTALL_DIR" merge-base --is-ancestor "$INSTALL_COMMIT" "origin/$BRANCH" 2>/dev/null \
            || fail "commit $INSTALL_COMMIT is not on branch $BRANCH"
        git -C "$INSTALL_DIR" checkout "$INSTALL_COMMIT" || fail "could not pin commit $INSTALL_COMMIT"
    fi
}

stage_venv() {
    # Keep the installer stage protocol; PM alone creates dependency environments.
    local boot_py
    bootstrap_python
    log "bootstrap Python ready; PM prepares the dependency environment"
}

# Tool-only bootstrap: acquire uv and Python before PM's own dependencies exist.
# The application dependency graph is never installed in this interpreter.
bootstrap_python() {
    ensure_uv
    local _py
    # Read packages.python.version by following object names and braces, not
    # indentation — same pre-Python reader contract as setup-hermes.sh's pin().
    _py="$(awk -F '"' '
        /^[[:space:]]*("[^"]+"[[:space:]]*:[[:space:]]*)?\{/ { path[++depth] = $2; next }
        /^[[:space:]]*\}[[:space:]]*,?[[:space:]]*$/ { delete path[depth--]; next }
        path[2] == "packages" && path[3] == "python" && $2 == "version" && depth == 3 { print $4; exit }
    ' "$INSTALL_DIR/pm/lock.json" | cut -d+ -f1 | cut -d. -f1,2)"
    [ -n "$_py" ] || _py="3.14"
    # Only base interpreters qualify: an activated app venv must not become
    # PM's bootstrap parent. Prefer the existing managed Python, then a host
    # Python of the same supported minor before attempting a download (#10778).
    # This interpreter only boots PM; PM still owns the exact runtime pin.
    if ! boot_py="$(UV_SYSTEM_PYTHON=1 UV_NO_PROJECT=1 "$UV_CMD" python find --managed-python "$_py" 2>/dev/null)" \
        && ! boot_py="$("$UV_CMD" python find --system --no-project "$_py" 2>/dev/null)"; then
        "$UV_CMD" python install --no-bin --no-registry "$_py" || fail "bootstrap Python installation failed"
        boot_py="$(UV_SYSTEM_PYTHON=1 UV_NO_PROJECT=1 "$UV_CMD" python find --managed-python "$_py")" || fail "bootstrap Python lookup failed"
    fi
    boot_py="${boot_py%$'\r'}"
    [ -x "$boot_py" ] && "$boot_py" --version >/dev/null 2>&1 || fail "bootstrap Python is not executable: $boot_py"
}

# uv exits before PM can replace its tool entry. pm.cli then prepares and
# enters its independently locked runtime before mutating application deps.
bootstrap_pm() {
    local boot_py
    bootstrap_python
    log "delegating python + venv + tools to pm (hash-verified via uv.lock)"
    (cd "$INSTALL_DIR" && "$boot_py" -m pm.cli install) || fail "pm install failed"
}

stage_python_deps() {
    bootstrap_pm
}

desktop_product_present() {
    # Does this checkout already carry a built desktop app? A plain repair or
    # upgrade rerun on a desktop install must REBUILD it rather than leave a
    # bundle built from the previous code: the app is part of that install, and
    # the artifacts live inside the tree (gitignored), so an update makes them
    # stale instead of removing them.
    local release="$INSTALL_DIR/apps/desktop/release"
    [ -d "$release/linux-unpacked" ] || [ -d "$release/mac" ] \
        || [ -d "$release/mac-arm64" ] || [ -d "$release/win-unpacked" ]
}

append_shell_path() {
    local rc="$1" line="$2" pattern="$3"
    # Existing user PATH setup wins; never append another line on a repair run.
    if [ -f "$rc" ] && grep -E "$pattern" "$rc" >/dev/null 2>&1; then
        return 0
    fi
    mkdir -p "$(dirname "$rc")"
    printf '\n# Hermes Agent command\n%s\n' "$line" >> "$rc" || fail "cannot update PATH in $rc"
    log "added ~/.local/bin to PATH in $rc"
}

wire_shell_path() {
    # The launcher is published by source_completion; shell rc files belong to
    # the installer, not to PM (updates must not modify a user's shell setup).
    # Never use the installer's inherited PATH as a proxy for a *new* shell.
    local login_shell="${SHELL:-/bin/bash}"
    case "${login_shell##*/}" in
        zsh)
            append_shell_path "$HOME/.zshrc" 'export PATH="$HOME/.local/bin:$PATH"' '^[[:space:]]*[^#[:space:]].*PATH=.*\.local/bin'
            append_shell_path "$HOME/.zprofile" 'export PATH="$HOME/.local/bin:$PATH"' '^[[:space:]]*[^#[:space:]].*PATH=.*\.local/bin'
            ;;
        fish)
            append_shell_path "$HOME/.config/fish/config.fish" 'fish_add_path "$HOME/.local/bin"' '^[[:space:]]*fish_add_path.*\.local/bin'
            ;;
        *)
            append_shell_path "$HOME/.bashrc" 'export PATH="$HOME/.local/bin:$PATH"' '^[[:space:]]*[^#[:space:]].*PATH=.*\.local/bin'
            append_shell_path "$HOME/.profile" 'export PATH="$HOME/.local/bin:$PATH"' '^[[:space:]]*[^#[:space:]].*PATH=.*\.local/bin'
            # Bash prefers .bash_profile over .profile if both exist.
            if [ -f "$HOME/.bash_profile" ]; then
                append_shell_path "$HOME/.bash_profile" 'export PATH="$HOME/.local/bin:$PATH"' '^[[:space:]]*[^#[:space:]].*PATH=.*\.local/bin'
            fi
            ;;
    esac
}

stage_products() {
    # The whole tail in one place, by calling the completion an update calls:
    # publish the commands, build the products (tui/web, plus the desktop app),
    # then run the post-build maintenance that syncs bundled skills and migrates
    # config. Node, browsers and the frontend build tools arrive through pm as
    # the build asks for them; the bootstrap interpreter itself only re-enters
    # the tree on PM's selected Python.
    local boot_py
    local args=(--source "$INSTALL_DIR")
    bootstrap_python
    if [ "$INCLUDE_DESKTOP" = true ] || desktop_product_present; then
        args+=(--desktop)
    fi
    (cd "$INSTALL_DIR" && "$boot_py" -I -B -X utf8 hermes_cli/source_completion.py "${args[@]}") \
        || fail "app products or command publication failed"
    wire_shell_path
    log "app products and hermes command ready"
}

stage_desktop() {
    # External-caller contract: `--stage desktop` stays dispatchable on its own
    # (the manifest never lists it now -- --include-desktop selects the desktop
    # product inside `products`). Same completion call, desktop selected.
    INCLUDE_DESKTOP=true
    stage_products
}

stage_config() {
    mkdir -p "$HERMES_HOME"/cron "$HERMES_HOME"/sessions "$HERMES_HOME"/logs \
        "$HERMES_HOME"/pairing "$HERMES_HOME"/hooks "$HERMES_HOME"/image_cache \
        "$HERMES_HOME"/audio_cache "$HERMES_HOME"/memories "$HERMES_HOME"/skills
    if [ ! -f "$HERMES_HOME/.env" ]; then
        cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env" 2>/dev/null || touch "$HERMES_HOME/.env"
    fi
    chmod 600 "$HERMES_HOME/.env"
    if [ ! -f "$HERMES_HOME/config.yaml" ] && [ -f "$INSTALL_DIR/cli-config.yaml.example" ]; then
        cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"
    fi
    log "config prepared in $HERMES_HOME"
}

# Interactive stages read the terminal, not stdin: under `curl | bash` stdin
# IS the script. Probe by opening /dev/tty -- a Docker build has the device
# node in its mount namespace but opening it fails (ENXIO).
has_terminal() { (: </dev/tty) 2>/dev/null; }

stage_setup() {
    if [ "$NON_INTERACTIVE" = true ]; then return 0; fi
    if ! has_terminal; then
        log "setup skipped (no terminal); run 'hermes setup' after install"
        return 0
    fi
    "$INSTALL_DIR/.hermes/bin/hermes" setup </dev/tty || fail "setup failed"
}

stage_gateway() {
    if [ "$NON_INTERACTIVE" = true ]; then return 0; fi
    if ! has_terminal; then
        log "gateway setup skipped (no terminal); run 'hermes gateway install' after install"
        return 0
    fi
    "$INSTALL_DIR/.hermes/bin/hermes" gateway install </dev/tty || fail "gateway installation failed"
}

stage_complete() {
    local commit
    commit="$INSTALL_COMMIT"
    [ -n "$commit" ] || commit=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null) || commit=""
    if [ -n "$commit" ]; then
        printf '{\n  "schemaVersion": 1,\n  "pinnedCommit": "%s",\n  "pinnedBranch": "%s",\n  "completedAt": "%s"\n}\n' \
            "$commit" "$BRANCH" "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" > "$INSTALL_DIR/.hermes-bootstrap-complete.tmp"
        mv -f "$INSTALL_DIR/.hermes-bootstrap-complete.tmp" "$INSTALL_DIR/.hermes-bootstrap-complete"
    fi
    log "install complete. Run: hermes"
}

run_stage() (
    # Keep failure handling out of conditional calls, which disable errexit.
    set -e
    STAGE="$1"
    STAGE_REASON=""
    STAGE_SKIPPED=false
    trap 'stage_result "$?"' EXIT
    if [ "$NON_INTERACTIVE" = true ] && { [ "$STAGE" = setup ] || [ "$STAGE" = gateway ]; }; then
        STAGE_SKIPPED=true
        STAGE_REASON="needs user input"
        exit 0
    fi
    case "$1" in
        prerequisites) stage_prerequisites ;;
        repository) stage_repository ;;
        venv) stage_venv ;;
        python-deps) stage_python_deps ;;
        config) stage_config ;;
        products) stage_products ;;
        setup) stage_setup ;;
        gateway) stage_gateway ;;
        desktop) stage_desktop ;;
        complete) stage_complete ;;
        *) STAGE_REASON="unknown stage: $1"; printf '%s\n' "$STAGE_REASON" >&2; exit 2 ;;
    esac
)

# Main. Guarded so the script can be SOURCED for its functions (the
# installer-test harness sources it with --manifest, which must define
# the functions and stop before main). Under `curl | bash` BASH_SOURCE is
# empty, and `set -u` would abort on the bare expansion.
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
    if [ "$WANT_MANIFEST" = true ]; then
        emit_manifest
        exit 0
    fi

    if [ -n "$STAGE" ] && [ "$JSON" = true ]; then
        trap 'stage_result "$?"' EXIT
    fi
    check_platform
    trap - EXIT

    if [ -n "$STAGE" ]; then
        run_stage "$STAGE"
        exit "$?"
    fi

    # No --stage: run the whole ladder — the same authoritative list the
    # manifest prints, so --include-desktop inserts desktop here too.
    for s in $(stage_names); do
        run_stage "$s"
        rc=$?
        [ "$rc" -eq 0 ] || exit "$rc"
    done
fi
