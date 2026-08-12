#!/usr/bin/env bash
# Prove a user who installed OLD via the installer script can reach HEAD.
#
# The POSIX sibling of tests/install/windows-desktop-gui-e2e.ps1, sharing its
# staging trick and replacing the old bubblewrap sandbox: instead of a fake
# Internet (MITM proxy + upload-pack shim), every git process is pointed at a
# local bare clone with url.<file://serve.git>.insteadOf rewrites for both
# canonical repo URLs in a driver-owned GIT_CONFIG_GLOBAL. The installer and
# updater run byte-for-byte against their real URLs and land on serve.git;
# `main` serves OLD during the install, then advances to HEAD for the update
# leg -- an update becomes available exactly the way it does for a real user.
# No bwrap, no slirp4netns, no TLS interception; the CI runner is disposable,
# so the host IS the sandbox.
#
# install.sh itself is not curl'd: the install leg runs the copy shipped AT
# the OLD ref (what a user who installed then actually executed), and the
# installer-script update leg runs HEAD's copy (what the website serves at
# update time).
#
# Phases (mirroring the windows driver):
#   stage      bare-clone this checkout to serve.git, park main at OLD
#   install    run OLD's scripts/install.sh under the redirect; assert the
#              install landed on OLD with a working `hermes`
#   update     advance served main to HEAD, apply ONE update method, assert
#              the checkout landed on HEAD with a working `hermes`
#
# Usage:
#   tests/install/installer-script-e2e.sh --update-method hermes-update|installer-script|installer-script+desktop
#                                         [--install-method installer-script|installer-script+desktop]
#                                         [--install-ref REF]
#
#   --install-method installer-script          the plain one-liner (default)
#                    installer-script+desktop  the one-liner with its desktop
#                                              stage opted in (--include-desktop)
#   --update-method  hermes-update      `hermes update`
#                    installer-script   re-run install.sh (HEAD's copy)
#                    installer-script+desktop  re-run with --include-desktop
#   --install-ref    what to install first; anything git resolves. Default:
#                    the newest release tag in the checkout.
#
# Requires a clean full-history checkout with release tags fetched.

set -euo pipefail

INSTALL_METHOD="installer-script"
UPDATE_METHOD=""
INSTALL_REF=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-method)
      [ "$#" -ge 2 ] || { echo 'error: --install-method needs a value' >&2; exit 1; }
      INSTALL_METHOD="$2"; shift 2 ;;
    --update-method)
      [ "$#" -ge 2 ] || { echo 'error: --update-method needs a value' >&2; exit 1; }
      UPDATE_METHOD="$2"; shift 2 ;;
    --install-ref)
      [ "$#" -ge 2 ] || { echo 'error: --install-ref needs a value' >&2; exit 1; }
      INSTALL_REF="$2"; shift 2 ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; exit 1 ;;
  esac
done
case "$INSTALL_METHOD" in
  installer-script|installer-script+desktop) ;;
  *) echo "error: --install-method must be installer-script or installer-script+desktop, got '$INSTALL_METHOD'" >&2; exit 1 ;;
esac
case "$UPDATE_METHOD" in
  hermes-update|installer-script|installer-script+desktop) ;;
  *) echo "error: --update-method must be hermes-update, installer-script or installer-script+desktop, got '$UPDATE_METHOD'" >&2; exit 1 ;;
esac

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REPO_URL_SSH="git@github.com:NousResearch/hermes-agent.git"
REPO_URL_HTTPS="https://github.com/NousResearch/hermes-agent.git"

# Everything lives OUTSIDE the checkout; an untracked dir inside the repo
# would make later dirty-tree checks lie.
WORK_ROOT="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/hermes-installer-script-e2e"
LOG_DIR="${HERMES_E2E_LOG_DIR:-$WORK_ROOT/logs}"
SERVE_REPO="$WORK_ROOT/serve.git"

step() { printf '\n=== %s ===\n' "$*"; }
ok()   { printf '  OK %s\n' "$*"; }
fail() { printf 'E2E ASSERTION FAILED: %s\n' "$*" >&2; exit 1; }
# Full transcript in the job log, collapsed (GitHub renders ::group:: as a
# fold; plain text anywhere else). Win or lose -- a green install's log is
# how you diagnose the leg that fails next.
log_group() {
  printf '::group::%s\n' "$1"
  cat "$2"
  printf '::endgroup::\n'
}

rm -rf "$WORK_ROOT"
mkdir -p "$WORK_ROOT" "$LOG_DIR"

# --- stage: serve.git with main parked at OLD --------------------------------

step "staging serve.git (main -> OLD)"
# Tracked changes only (-uno): the bare clone serves committed objects, so a
# modified tracked file means HEAD is not the code being reviewed -- but an
# untracked file (scratch notes, this driver before it lands) cannot leak
# into the clone at all.
[ -z "$(git -C "$REPO_ROOT" status --porcelain -uno)" ] \
  || fail "checkout has uncommitted tracked changes; the staged clone must be a reviewable commit"

if [ -z "$INSTALL_REF" ]; then
  INSTALL_REF="$(git -C "$REPO_ROOT" tag --list 'v[0-9]*' --sort=-creatordate | head -1)"
  [ -n "$INSTALL_REF" ] || fail "no release tags in the checkout to use as OLD"
fi
OLD_SHA="$(git -C "$REPO_ROOT" rev-parse "${INSTALL_REF}^{commit}")"
HEAD_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
[ "$OLD_SHA" != "$HEAD_SHA" ] || fail "OLD ($INSTALL_REF) IS HEAD; no update would be available"

git clone --bare --quiet "$REPO_ROOT" "$SERVE_REPO"
git -C "$SERVE_REPO" update-ref refs/heads/main "$OLD_SHA"
git -C "$SERVE_REPO" symbolic-ref HEAD refs/heads/main
# The installer may pin a commit that is reachable but not at a ref tip.
git -C "$SERVE_REPO" config uploadpack.allowAnySHA1InWant true
ok "serve.git main = $OLD_SHA ($INSTALL_REF), update target $HEAD_SHA"

# --- the git URL redirect -----------------------------------------------------

# A driver-owned global gitconfig, NOT GIT_CONFIG_COUNT/KEY_n/VALUE_n env
# config: install.sh sets those itself and would clobber ours.
GIT_CFG="$WORK_ROOT/gitconfig"
cat > "$GIT_CFG" <<EOF
[url "file://$SERVE_REPO"]
	insteadOf = $REPO_URL_HTTPS
	insteadOf = $REPO_URL_SSH
EOF
export GIT_CONFIG_GLOBAL="$GIT_CFG"
ok "git URL redirect via GIT_CONFIG_GLOBAL=$GIT_CFG"

# Isolated HOME: the runner's real one may carry a preinstalled hermes or a
# developer config, and old installer scripts hardcode $HOME/.hermes (the
# HERMES_HOME env override is newer than tags we sample). GIT_CONFIG_GLOBAL
# above keeps working -- an explicit path wins over $HOME/.gitconfig.
export HOME="$WORK_ROOT/home"
mkdir -p "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"
export HERMES_HOME="$HOME/.hermes"
mkdir -p "$HERMES_HOME"
# serve.git's file:// origin looks like a fork to the updater, whose "add the
# official repo as upstream?" prompt would hang a headless run. This marker is
# the product's own mechanism for suppressing it.
touch "$HERMES_HOME/.skip_upstream_prompt"

INSTALL_DIR="$HERMES_HOME/hermes-agent"

# Does the installer script at REF accept FLAG? Read that ref's own
# install.sh rather than assuming this checkout's flag set: the point of the
# matrix is to install releases from months back, whose installers predate
# options we take for granted.
installer_supports() {
  git -C "$REPO_ROOT" show "$1:scripts/install.sh" | grep -qF -- "$2"
}

run_installer() {
  # $1: ref whose scripts/install.sh to run; $2: log name; $3: "desktop" to
  # opt the desktop stage in (--include-desktop)
  local script="$WORK_ROOT/install-$2.sh"
  git -C "$REPO_ROOT" show "$1:scripts/install.sh" > "$script"
  chmod +x "$script"
  # Installer flags have to match the installer being run, not this
  # checkout's: older releases reject options added later. --skip-setup goes
  # back further than any tag we sample; anything newer is probed for.
  local flags=(--skip-setup)
  if installer_supports "$1" "--skip-browser"; then
    flags+=(--skip-browser)
  fi
  if [ "${3:-}" = "desktop" ]; then
    # The desktop stage is the point of this leg, so a ref without the
    # flag is a hard failure, not a silent downgrade to a plain install.
    # (Releases that predate apps/desktop are already skipped upstream by
    # the tag-has-desktop gate; the flag shipped with the app.)
    installer_supports "$1" "--include-desktop" \
      || fail "ref $1 does not support --include-desktop; this leg cannot mean what it claims"
    flags+=(--include-desktop)
  fi
  # </dev/null: the script reads prompts from stdin when a tty is absent;
  # EOF makes every remaining prompt take its default.
  local rc=0
  bash "$script" "${flags[@]}" < /dev/null > "$LOG_DIR/install-$2.log" 2>&1 || rc=$?
  log_group "install.sh ($2) transcript" "$LOG_DIR/install-$2.log"
  [ "$rc" -eq 0 ] || fail "install.sh ($2) exited $rc; transcript above, log at $LOG_DIR/install-$2.log"
}

assert_desktop_artifact() {
  # $1: label. After a +desktop install the built app must exist under the
  # checkout -- install.sh builds it there and registers no OS entry point.
  local release_dir="$INSTALL_DIR/apps/desktop/release"
  local found=""
  local cand
  for cand in \
    "$release_dir/linux-unpacked/Hermes" \
    "$release_dir/linux-unpacked/hermes" \
    "$release_dir/mac-arm64/Hermes.app" \
    "$release_dir/mac/Hermes.app"; do
    if [ -x "$cand" ] || [ -d "$cand" ]; then
      found="$cand"
      break
    fi
  done
  [ -n "$found" ] || fail "no desktop app under $release_dir after $1 (+desktop install)"
  ok "desktop app built by installer at $1: $found"
}

assert_checkout() {
  # $1: expected sha, $2: label
  local got
  got="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
  [ "$got" = "$1" ] || fail "installed checkout is $got, expected $2 ($1)"
  ok "checkout is $2 ($1)"
  local hermes="$INSTALL_DIR/venv/bin/hermes"
  [ -x "$hermes" ] || fail "no hermes console script at $hermes"
  "$hermes" --version > "$LOG_DIR/version-$2.log" 2>&1 \
    || fail "hermes --version failed after $2; log in $LOG_DIR/version-$2.log"
  ok "hermes --version works: $(head -c 120 "$LOG_DIR/version-$2.log" | tr -d '\n')"
}

smoke_desktop() {
  # $1: label (old|head). Prove the installed CLI can produce the desktop
  # app: `hermes desktop --build-only` runs the full desktop pipeline
  # (workspace install, renderer build, stamp write) and stops before the
  # launch -- the same call `hermes update` itself makes. Probe the
  # INSTALLED hermes for the flag rather than assuming this checkout's
  # surface: sampled OLD releases may predate `hermes desktop` or
  # --build-only entirely, and for them the phase skips, loudly.
  local hermes="$INSTALL_DIR/venv/bin/hermes"
  if ! "$hermes" desktop --help 2>/dev/null | grep -qF -- --build-only; then
    ok "hermes desktop --build-only not supported at $1; skipping desktop smoke"
    return 0
  fi
  local rc=0
  (cd "$INSTALL_DIR" && "$hermes" desktop --build-only < /dev/null \
    > "$LOG_DIR/desktop-smoke-$1.log" 2>&1) || rc=$?
  log_group "hermes desktop --build-only ($1) transcript" "$LOG_DIR/desktop-smoke-$1.log"
  [ "$rc" -eq 0 ] || fail "hermes desktop --build-only ($1) exited $rc; transcript above"
  ok "hermes desktop --build-only works at $1"
  # TODO(launch): LAUNCH the built app and auto-close it. Mechanism when
  # the pieces land: driver-side spawn interception (a sitecustomize.py on
  # PYTHONPATH wraps subprocess.run under an env-var opt-in and captures
  # the real argv/cwd/env at the spawn site) + Playwright _electron.launch
  # on the captured spec; electronApp.close() is the auto-close. Blocked
  # on that asset and, for linux runners, on a virtual display (Xvfb).
}

# --- install OLD ---------------------------------------------------------------

step "installing OLD ($INSTALL_REF) via its own scripts/install.sh ($INSTALL_METHOD)"
if [ "$INSTALL_METHOD" = "installer-script+desktop" ]; then
  run_installer "$OLD_SHA" old desktop
  assert_checkout "$OLD_SHA" OLD
  assert_desktop_artifact OLD
else
  run_installer "$OLD_SHA" old
  assert_checkout "$OLD_SHA" OLD
fi
smoke_desktop old

# --- update OLD -> HEAD ----------------------------------------------------------

step "advancing served main to HEAD"
git -C "$SERVE_REPO" update-ref refs/heads/main "$HEAD_SHA"
ok "serve.git main = $HEAD_SHA"

step "updating via $UPDATE_METHOD"
case "$UPDATE_METHOD" in
  hermes-update)
    # `--yes` reaches the update subcommand only in later releases, and
    # argparse rejects the whole invocation when it does not exist. Ask the
    # installed hermes; older ones read the prompt from stdin, so close it.
    HERMES="$INSTALL_DIR/venv/bin/hermes"
    if "$HERMES" update --help 2>&1 | grep -qF -- --yes; then
      update_cmd=("$HERMES" update --yes)
    else
      update_cmd=("$HERMES" update)
    fi
    rc=0
    (cd "$INSTALL_DIR" && "${update_cmd[@]}" < /dev/null > "$LOG_DIR/update.log" 2>&1) || rc=$?
    log_group "hermes update transcript" "$LOG_DIR/update.log"
    [ "$rc" -eq 0 ] || fail "hermes update exited $rc; transcript above, log at $LOG_DIR/update.log"
    ;;
  installer-script)
    # A user re-running the one-liner today gets the CURRENT script.
    run_installer "$HEAD_SHA" head
    ;;
  installer-script+desktop)
    run_installer "$HEAD_SHA" head desktop
    assert_desktop_artifact HEAD
    ;;
esac
assert_checkout "$HEAD_SHA" HEAD
smoke_desktop head

step "PASS: $INSTALL_REF -> HEAD via $UPDATE_METHOD"
