# Prove a Windows user who installed OLD via the installer script (irm |
# iex) can reach HEAD.
#
# The install.ps1 sibling of tests/install/installer-script-e2e.sh, sharing
# its staging trick with the GUI driver (windows-desktop-gui-e2e.ps1):
# every git process is pointed at a local bare clone with
# url.<file://serve.git>.insteadOf rewrites for both canonical repo URLs in
# a driver-owned GIT_CONFIG_GLOBAL. The installer and updater run
# byte-for-byte against their real URLs and land on serve.git; `main`
# serves OLD during the install, then advances to HEAD for the update leg.
# (NOT GIT_CONFIG_COUNT/KEY_n/VALUE_n env config -- install.ps1 SETS those
# itself and would clobber ours.)
#
# install.ps1 itself is not downloaded: the install leg runs the copy
# shipped AT the OLD ref (what a user who installed then actually
# executed), and the installer-script update leg runs HEAD's copy (what
# the website serves at update time).
#
# Usage:
#   powershell -File tests\install\windows-installer-script-e2e.ps1 `
#     -UpdateMethod hermes-update -InstallRef v0.20.2
#
#   -UpdateMethod  hermes-update      venv\Scripts\hermes.exe update
#                  installer-script   re-run install.ps1 (HEAD's copy)
#   -InstallRef    what to install first; anything git resolves. auto =
#                  the newest release tag in the checkout.
#
# Requires a clean full-history checkout with release tags fetched.
# PowerShell 5.1-safe, pure ASCII (OEM codepages explode on fancy dashes).

#Requires -Version 5.1

param(
    [ValidateSet("hermes-update", "installer-script")]
    [string]$UpdateMethod = "hermes-update",
    [string]$InstallRef = "auto"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RepoUrlSsh = "git@github.com:NousResearch/hermes-agent.git"
$RepoUrlHttps = "https://github.com/NousResearch/hermes-agent.git"

# Everything lives OUTSIDE the checkout; an untracked dir inside the repo
# would trip the dirty-tree guard below on the next run.
$WorkRoot = Join-Path $(if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }) "hermes-installer-script-e2e"
$LogDir = if ($env:HERMES_E2E_LOG_DIR) { $env:HERMES_E2E_LOG_DIR } else { Join-Path $WorkRoot "logs" }
$ServeRepo = Join-Path $WorkRoot "serve.git"

function Step([string]$Message) { Write-Host "`n=== $Message ===" }
function Ok([string]$Message) { Write-Host "  OK $Message" }
function Fail([string]$Message) {
    Write-Host "E2E ASSERTION FAILED: $Message" -ForegroundColor Red
    exit 1
}

function Invoke-Git {
    param([string[]]$GitArgs)
    $out = & git @GitArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        Fail "git $($GitArgs -join ' ') exited $LASTEXITCODE`: $out"
    }
    return $out
}

if (Test-Path -LiteralPath $WorkRoot) { Remove-Item -Recurse -Force -LiteralPath $WorkRoot }
New-Item -ItemType Directory -Path $WorkRoot, $LogDir -Force | Out-Null

# --- stage: serve.git with main parked at OLD --------------------------------

Step "staging serve.git (main -> OLD)"
# Tracked changes only (-uno): untracked files cannot leak into a bare clone.
$dirty = Invoke-Git @("-C", $RepoRoot, "status", "--porcelain", "-uno")
if ($dirty) { Fail "checkout has uncommitted tracked changes; the staged clone must be a reviewable commit" }

if ($InstallRef -eq "auto") {
    $tags = (Invoke-Git @("-C", $RepoRoot, "tag", "--list", "v[0-9]*", "--sort=-creatordate")) -split "`r?`n"
    $InstallRef = $tags | Select-Object -First 1
    if (-not $InstallRef) { Fail "no release tags in the checkout to use as OLD" }
}
$OldSha = (Invoke-Git @("-C", $RepoRoot, "rev-parse", "$InstallRef^{commit}")).Trim()
$HeadSha = (Invoke-Git @("-C", $RepoRoot, "rev-parse", "HEAD")).Trim()
if ($OldSha -eq $HeadSha) { Fail "OLD ($InstallRef) IS HEAD; no update would be available" }

Invoke-Git @("clone", "--bare", "--quiet", $RepoRoot, $ServeRepo) | Out-Null
Invoke-Git @("-C", $ServeRepo, "update-ref", "refs/heads/main", $OldSha) | Out-Null
Invoke-Git @("-C", $ServeRepo, "symbolic-ref", "HEAD", "refs/heads/main") | Out-Null
# The installer may pin a commit that is reachable but not at a ref tip.
Invoke-Git @("-C", $ServeRepo, "config", "uploadpack.allowAnySHA1InWant", "true") | Out-Null
Ok "serve.git main = $OldSha ($InstallRef), update target $HeadSha"

# --- the git URL redirect ------------------------------------------------------

$gitCfg = Join-Path $WorkRoot "gitconfig"
$serveUrl = "file:///" + ($ServeRepo -replace "\\", "/")
@"
[url "$serveUrl"]
	insteadOf = $RepoUrlHttps
	insteadOf = $RepoUrlSsh
"@ | Set-Content -LiteralPath $gitCfg -Encoding Ascii
$env:GIT_CONFIG_GLOBAL = $gitCfg
Ok "git URL redirect via GIT_CONFIG_GLOBAL=$gitCfg"

# Isolated install target: the runner may carry a preinstalled hermes.
# -HermesHome/-InstallDir are passed explicitly because the oldest sampled
# installers predate the HERMES_HOME env override.
$HermesHome = Join-Path $WorkRoot "hermes-home"
$InstallDir = Join-Path $HermesHome "hermes-agent"
New-Item -ItemType Directory -Path $HermesHome -Force | Out-Null
$env:HERMES_HOME = $HermesHome
# serve.git's file:// origin looks like a fork to the updater, whose "add
# the official repo as upstream?" prompt would hang a headless run. This
# marker is the product's own mechanism for suppressing it.
Set-Content -LiteralPath (Join-Path $HermesHome ".skip_upstream_prompt") -Value "" -Encoding Ascii

function Invoke-Installer {
    param([string]$Ref, [string]$Label)
    $script = Join-Path $WorkRoot "install-$Label.ps1"
    (Invoke-Git @("-C", $RepoRoot, "show", "$Ref`:scripts/install.ps1")) -join "`n" |
        Set-Content -LiteralPath $script -Encoding UTF8
    # Flags must match the installer being run, not this checkout's: older
    # releases reject parameters added later. -SkipSetup/-HermesHome/
    # -InstallDir go back further than any tag we sample; -NonInteractive
    # is probed from the ref's own script text.
    $flags = @("-SkipSetup", "-HermesHome", $HermesHome, "-InstallDir", $InstallDir)
    $text = Get-Content -LiteralPath $script -Raw
    if ($text -match '\$NonInteractive') { $flags += "-NonInteractive" }
    $log = Join-Path $LogDir "install-$Label.log"
    # Native stderr (git clone progress, pip notices) must not become
    # terminating NativeCommandErrors under EAP=Stop; the exit code is the
    # verdict here, not stderr chatter.
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    & powershell -NoProfile -ExecutionPolicy Bypass -File $script @flags *> $log
    $installExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    if ($installExit -ne 0) {
        Get-Content -LiteralPath $log -Tail 50 | Write-Host
        Fail "install.ps1 ($Label) exited $installExit; full log in $log"
    }
}

function Assert-Checkout {
    param([string]$ExpectedSha, [string]$Label)
    $got = (Invoke-Git @("-C", $InstallDir, "rev-parse", "HEAD")).Trim()
    if ($got -ne $ExpectedSha) { Fail "installed checkout is $got, expected $Label ($ExpectedSha)" }
    Ok "checkout is $Label ($ExpectedSha)"
    $hermes = Join-Path $InstallDir "venv\Scripts\hermes.exe"
    if (-not (Test-Path -LiteralPath $hermes)) { Fail "no hermes console script at $hermes" }
    $verLog = Join-Path $LogDir "version-$Label.log"
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    & $hermes --version *> $verLog
    $verExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    if ($verExit -ne 0) {
        Get-Content -LiteralPath $verLog | Write-Host
        Fail "hermes --version failed after $Label; log in $verLog"
    }
    Ok "hermes --version works: $((Get-Content -LiteralPath $verLog -First 1))"
}

# --- install OLD ------------------------------------------------------------------

Step "installing OLD ($InstallRef) via its own scripts/install.ps1"
Invoke-Installer $OldSha "old"
Assert-Checkout $OldSha "OLD"

# --- update OLD -> HEAD --------------------------------------------------------------

Step "advancing served main to HEAD"
Invoke-Git @("-C", $ServeRepo, "update-ref", "refs/heads/main", $HeadSha) | Out-Null
Ok "serve.git main = $HeadSha"

Step "updating via $UpdateMethod"
switch ($UpdateMethod) {
    "hermes-update" {
        $hermes = Join-Path $InstallDir "venv\Scripts\hermes.exe"
        # `--yes` reaches the update subcommand only in later releases, and
        # argparse rejects the whole invocation when it does not exist.
        $updateArgs = @("update")
        $prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
        $helpText = & $hermes update --help 2>&1 | Out-String
        if ($helpText -match '--yes') { $updateArgs += "--yes" }
        $log = Join-Path $LogDir "update.log"
        Push-Location $InstallDir
        try {
            & $hermes @updateArgs *> $log
            $updateExit = $LASTEXITCODE
        } finally {
            Pop-Location
            $ErrorActionPreference = $prevEap
        }
        if ($updateExit -ne 0) {
            Get-Content -LiteralPath $log -Tail 50 | Write-Host
            Fail "hermes update exited $updateExit; full log in $log"
        }
    }
    "installer-script" {
        # A user re-running the one-liner today gets the CURRENT script.
        Invoke-Installer $HeadSha "head"
    }
}
Assert-Checkout $HeadSha "HEAD"

Step "PASS: $InstallRef -> HEAD via $UpdateMethod"
