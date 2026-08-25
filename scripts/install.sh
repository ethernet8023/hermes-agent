#!/usr/bin/env bash
# Hermes Agent bootstrap: git checkout + venv + hermes command on PATH.
# Heavy dependencies (tool binaries, browsers, node) are pm's job after
# this: `hermes pm install`. Stage protocol kept for Hermes-Setup:
#   --manifest            print the stage list as JSON
#   --stage NAME [--json] run one stage
#   --non-interactive     skip stages that need input
#   --include-desktop     add the desktop build stage
set -u

REPO_URL="${HERMES_REPO_URL:-https://github.com/NousResearch/hermes-agent.git}"
BRANCH="main"
INSTALL_COMMIT=""
INSTALL_DIR="${HERMES_INSTALL_DIR:-$HOME/.hermes/hermes-agent}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
STAGE=""
WANT_MANIFEST=false
JSON=false
NON_INTERACTIVE=false
INCLUDE_DESKTOP=false

while [ $# -gt 0 ]; do
    case "$1" in
        --branch|-Branch) BRANCH="$2"; shift 2 ;;
        --commit|-Commit) INSTALL_COMMIT="$2"; shift 2 ;;
        --dir) INSTALL_DIR="$2"; shift 2 ;;
        --manifest|-Manifest) WANT_MANIFEST=true; shift ;;
        --stage|-Stage) STAGE="$2"; shift 2 ;;
        --json|-Json) JSON=true; shift ;;
        --non-interactive|-NonInteractive) NON_INTERACTIVE=true; shift ;;
        --skip-setup|--skip-browser) NON_INTERACTIVE=true; shift ;;
        --include-desktop|-IncludeDesktop) INCLUDE_DESKTOP=true; shift ;;
        -h|--help)
            echo "Usage: install.sh [--branch NAME] [--commit SHA] [--dir PATH]"
            echo "                  [--manifest] [--stage NAME] [--json]"
            echo "                  [--non-interactive] [--include-desktop]"
            exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

log() { printf "\033[1;34m[hermes]\033[0m %s\n" "$1"; }
fail() { printf "\033[1;31m[hermes]\033[0m %s\n" "$1" >&2; exit 1; }

check_platform() {
    case "$(uname -s 2>/dev/null)" in
        Linux*)
            if [ -n "${TERMUX_VERSION:-}" ] || [ -d "/data/data/com.termux" ]; then
                fail "Termux is not a supported install target."
            fi ;;
        Darwin*) : ;;
        *) fail "unsupported platform: $(uname -s). On Windows use install.ps1." ;;
    esac
}

json_frame() {
    # $1 ok, $2 stage, $3 skipped, $4 reason
    if [ -n "${4:-}" ]; then
        printf '{"ok":%s,"stage":"%s","skipped":%s,"reason":"%s"}\n' "$1" "$2" "$3" "$4"
    else
        printf '{"ok":%s,"stage":"%s","skipped":%s}\n' "$1" "$2" "$3"
    fi
}

emit_manifest() {
    local desktop=""
    if [ "$INCLUDE_DESKTOP" = true ]; then
        desktop='{"name":"desktop","title":"Build desktop app","category":"runtime","needs_user_input":false},'
    fi
    printf '%s' '{"protocol_version":1,"stages":['
    printf '%s' '{"name":"prerequisites","title":"System prerequisites","category":"runtime","needs_user_input":false},'
    printf '%s' '{"name":"repository","title":"Download Hermes Agent","category":"runtime","needs_user_input":false},'
    printf '%s' '{"name":"venv","title":"Create Python environment","category":"runtime","needs_user_input":false},'
    printf '%s' '{"name":"python-deps","title":"Install Python dependencies","category":"runtime","needs_user_input":false},'
    printf '%s' '{"name":"node-deps","title":"Install tool dependencies","category":"runtime","needs_user_input":false},'
    printf '%s' '{"name":"path","title":"Install hermes command","category":"runtime","needs_user_input":false},'
    printf '%s' '{"name":"config","title":"Prepare config and skills","category":"configuration","needs_user_input":false},'
    printf '%s' '{"name":"setup","title":"Configure API keys and settings","category":"configuration","needs_user_input":true},'
    printf '%s' '{"name":"gateway","title":"Configure gateway service","category":"configuration","needs_user_input":true},'
    printf '%s' "$desktop"
    printf '%s\n' '{"name":"complete","title":"Finish install","category":"runtime","needs_user_input":false}]}'
}

stage_prerequisites() {
    command -v git >/dev/null 2>&1 || fail "git is required. Install it with your system package manager."
    command -v curl >/dev/null 2>&1 || fail "curl is required. Install it with your system package manager."
    log "prerequisites ok (git, curl)"
}

stage_repository() {
    if [ -d "$INSTALL_DIR/.git" ]; then
        log "updating $INSTALL_DIR"
        git -C "$INSTALL_DIR" fetch origin "$BRANCH" || fail "git fetch failed"
        git -C "$INSTALL_DIR" checkout "$BRANCH" || fail "git checkout failed"
        git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH" || log "not fast-forwardable; keeping local state"
    else
        log "cloning $REPO_URL ($BRANCH) into $INSTALL_DIR"
        mkdir -p "$(dirname "$INSTALL_DIR")"
        git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" || fail "git clone failed"
    fi
    if [ -n "$INSTALL_COMMIT" ]; then
        git -C "$INSTALL_DIR" checkout "$INSTALL_COMMIT" || fail "could not pin commit $INSTALL_COMMIT"
    fi
}

stage_venv() {
    command -v uv >/dev/null 2>&1 || {
        log "installing uv (astral.sh)"
        curl -LsSf https://astral.sh/uv/install.sh | sh || fail "uv install failed"
        export PATH="$HOME/.local/bin:$PATH"
    }
    log "creating venv"
    (cd "$INSTALL_DIR" && uv venv --allow-existing venv) || fail "uv venv failed"
}

stage_python_deps() {
    log "syncing python dependencies (uv sync --frozen)"
    (cd "$INSTALL_DIR" && VIRTUAL_ENV="$INSTALL_DIR/venv" uv sync --frozen --extra all --active) || fail "uv sync failed"
}

stage_node_deps() {
    # Tool binaries, node, browsers: pm packages, installed on demand or
    # via `hermes pm install`. Nothing to do at bootstrap time.
    log "tool dependencies are managed by pm (hermes pm install)"
}

stage_path() {
    local link_dir="$HOME/.local/bin"
    mkdir -p "$link_dir"
    rm -f "$link_dir/hermes"
    {
        echo '#!/usr/bin/env bash'
        echo 'unset PYTHONPATH'
        echo 'unset PYTHONHOME'
        echo "exec \"$INSTALL_DIR/venv/bin/python\" \"$INSTALL_DIR/hermes\" \"\$@\""
    } > "$link_dir/hermes"
    chmod +x "$link_dir/hermes"
    case ":$PATH:" in
        *":$link_dir:"*) : ;;
        *) log "add $link_dir to your PATH to use the hermes command" ;;
    esac
    log "hermes command installed at $link_dir/hermes"
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

stage_setup() {
    if [ "$NON_INTERACTIVE" = true ]; then return 0; fi
    "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/hermes" setup || true
}

stage_gateway() {
    if [ "$NON_INTERACTIVE" = true ]; then return 0; fi
    "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/hermes" gateway install || true
}

stage_desktop() {
    "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/hermes" desktop build || fail "desktop build failed"
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

run_stage() {
    case "$1" in
        prerequisites) stage_prerequisites ;;
        repository) stage_repository ;;
        venv) stage_venv ;;
        python-deps) stage_python_deps ;;
        node-deps) stage_node_deps ;;
        path) stage_path ;;
        config) stage_config ;;
        setup) stage_setup ;;
        gateway) stage_gateway ;;
        desktop) stage_desktop ;;
        complete) stage_complete ;;
        *) echo "unknown stage: $1" >&2; exit 2 ;;
    esac
}

if [ "$WANT_MANIFEST" = true ]; then
    emit_manifest
    exit 0
fi

check_platform

if [ -n "$STAGE" ]; then
    if [ "$NON_INTERACTIVE" = true ] && { [ "$STAGE" = setup ] || [ "$STAGE" = gateway ]; }; then
        [ "$JSON" = true ] && json_frame true "$STAGE" true "needs user input"
        exit 0
    fi
    if run_stage "$STAGE"; then
        [ "$JSON" = true ] && json_frame true "$STAGE" false
        exit 0
    else
        rc=$?
        [ "$JSON" = true ] && json_frame false "$STAGE" false "stage failed"
        exit "$rc"
    fi
fi

# No --stage: run the whole ladder.
for s in prerequisites repository venv python-deps node-deps path config setup gateway complete; do
    run_stage "$s"
done
