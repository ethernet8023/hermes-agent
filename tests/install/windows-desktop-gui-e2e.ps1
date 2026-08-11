# ============================================================================
# Windows Desktop GUI install + update E2E driver (the REAL user flow)
# ============================================================================
# Proves, on a real Windows machine, that a user who installs Hermes the way
# the website tells them to can then update to the commit under test through
# a real update surface -- with every leg driven through the GUI a user
# actually touches:
#
#   INSTALL   - downloads the production Hermes-Setup.exe from the website,
#               launches it HEADED, and AutoHotkey clicks Install, waits,
#               then clicks Launch. The real Electron Hermes.exe must appear.
#               The exe runs EXACTLY as shipped against serve.git, whose
#               `main` is parked at OLD (-InstallRef, default: the newest
#               release tag) -- so the install lands on OLD the same way a
#               user's install landed on whatever main served that day.
#   UPDATE    - OLD -> HEAD through the route selected by -Route:
#                 desktop    (implemented) launch the installed Hermes.exe
#                            under Playwright's Electron driver and CLICK
#                            Settings -> About -> "Update now". The
#                            production hand-off chain runs untouched:
#                            marker, app quit, detached updater, `hermes
#                            update`, desktop rebuild, RELAUNCH. Asserts
#                            target sha, marker cleanup, result JSON (when
#                            the script path wrote one), working hermes,
#                            and the relaunched app window.
#                 update     TODO: run `hermes update` from the installed
#                            venv (the CLI route a GUI user might take).
#                 installer  TODO: re-run the bootstrap installer over the
#                            existing install (its --update flow).
#
# HOW THE STAGING WORKS (no MITM proxy, no network fakery):
#   We bare-clone the checkout into <workroot>\serve.git and point every git
#   process at it with url.<file-url>.insteadOf rewrites for the two
#   canonical repo URLs, via a driver-owned gitconfig selected with
#   GIT_CONFIG_GLOBAL. (NOT GIT_CONFIG_COUNT/KEY_n/VALUE_n env config --
#   install.ps1 sets those itself and silently clobbers them.) The
#   installer's `git clone` and `hermes update`'s `git fetch origin`
#   transparently hit OUR bare repo. Its `main` serves OLD for the install
#   phase; the update phase advances it to HEAD -- an update becomes
#   available exactly the way it does for a real user. Installer and
#   updater run byte-for-byte as shipped; everything else (uv, PyPI, npm,
#   the installer's raw.githubusercontent install.ps1 download) uses the
#   real network, same as a user install.
#
# PROOF: screenshots at every renderer step (Playwright), full-desktop
# screenshots around the installer/AHK phases, a rolling desktop capture
# (every 3s) plus a continuous ffmpeg screen recording (recording.mkv) for
# both phases, ahk.log, and the hand-off log. All uploaded as CI artifacts.
#
# DEVIATIONS FROM PRODUCTION (each one deliberate and small):
#   * the git URL redirect itself -- the staging requirement.
#   * serve.git gets uploadpack.allowAnySHA1InWant=true so the installer's
#     baked -Commit pin can be fetched from the redirected clone the same
#     way GitHub's upload-pack allows it.
#   * A dummy provider key is seeded after install so the update leg sees
#     the ready app shell instead of the onboarding overlay (a real
#     updating user has a configured provider).
#   * .skip_upstream_prompt is set: serve.git's file:// origin looks like a
#     fork to `hermes update`, whose fork-only upstream prompt is a bare
#     input() that hangs forever under the desktop's detached console.
#     Real GUI users are on the official origin and never see it.
#
# USAGE (local Windows box or CI):
#   powershell -File tests\install\windows-desktop-gui-e2e.ps1 -Phase all
#   ... -Phase stage / install-gui / update-gui
#   Phases share state via <workroot>\shas.json, so CI can run them as
#   separate steps for readable logs.
# ============================================================================

param(
    [ValidateSet("stage", "install-gui", "update-gui", "all")]
    [string]$Phase = "all",

    # Update method to exercise in the update-gui phase, named by the same
    # ids install-e2e.yml's combination jobs use. Only "desktop-app" (the
    # app's own Update button) is implemented; the others are declared arms
    # so the surface is stable when they land.
    [ValidateSet("desktop-app", "hermes-update", "desktop-installer-rerun@latest", "irm-iex")]
    [string]$Route = "desktop-app",

    # The OLD version: the ref served as `main` while the installer runs,
    # i.e. what the user starts on. The published Hermes-Setup.exe carries
    # no commit pin (Pin { commit: None, branch: "main" }) -- it installs
    # whatever `main` points at, so staging OLD means serving it there.
    # Empty or "auto" = newest release tag in the checkout (the "user on
    # the current release" starting point, same philosophy as the linux
    # axis's tag matrix). "auto" exists because `powershell -File` silently
    # swallows an empty-string argument ('Missing an argument for
    # parameter'), so the workflow cannot pass "".
    [string]$InstallRef = "auto",

    # Repo checkout whose HEAD is the update target.
    [string]$RepoRoot = "",

    [string]$WorkRoot = $(if ($env:HERMES_E2E_WORKROOT) { $env:HERMES_E2E_WORKROOT } else { Join-Path $env:TEMP "hermes-desktop-gui-e2e" }),

    [string]$SetupExeUrl = "https://hermes-assets.nousresearch.com/Hermes-Setup.exe"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

$ServeRepo   = Join-Path $WorkRoot "serve.git"
$HermesHome  = Join-Path $WorkRoot "hermes-home"
$InstallDir  = Join-Path $HermesHome "hermes-agent"
$StatePath   = Join-Path $WorkRoot "shas.json"
$ProofRoot   = Join-Path $WorkRoot "proof"
$AhkDir      = Join-Path $WorkRoot "ahk"
$AssetsDir   = Join-Path $PSScriptRoot "e2e-assets"

$RepoUrlHttps = "https://github.com/NousResearch/hermes-agent.git"
$RepoUrlSsh   = "git@github.com:NousResearch/hermes-agent.git"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host ("=" * 74)
    Write-Host "== $Message"
    Write-Host ("=" * 74)
}

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) {
        throw "E2E ASSERTION FAILED: $Message"
    }
    Write-Host "  [ok] $Message"
}

function Invoke-Git([string[]]$GitArgs) {
    # PS 5.1 trap: under $ErrorActionPreference = "Stop", a native command
    # that writes ANYTHING to stderr while merged via 2>&1 throws a
    # NativeCommandError even when it exits 0 (git loves stderr for
    # progress/notices). Relax EAP around the native call only; exit-code
    # checking below is the real error gate.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & git @GitArgs 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE): $output"
        }
        return ($output | Out-String).Trim()
    } finally {
        $ErrorActionPreference = $prevEap
    }
}

function Set-GitRedirect {
    # Route the canonical repo URLs to the local bare repo for THIS process
    # and every child (the installer's git, hermes update's git).
    #
    # MECHANISM: a driver-owned global gitconfig selected via
    # GIT_CONFIG_GLOBAL. Do NOT use GIT_CONFIG_COUNT/KEY_n/VALUE_n env
    # config here -- install.ps1 SETS those itself (GIT_CONFIG_COUNT=1,
    # windows.appendAtomically), silently clobbering any redirect we put
    # there. install.ps1's own `git config --global` writes simply land in
    # our file, so its compat settings still apply. Nothing leaks onto the
    # machine: the file lives in the workroot and dies with it.
    $fileUrl = "file:///" + ($ServeRepo -replace "\\", "/")
    $gitCfg = Join-Path $WorkRoot "e2e-gitconfig"
    if (-not (Test-Path -LiteralPath $WorkRoot)) {
        New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null
    }
    @"
[url "$fileUrl"]
	insteadOf = $RepoUrlHttps
	insteadOf = $RepoUrlSsh
"@ | Set-Content -LiteralPath $gitCfg -Encoding ASCII
    $env:GIT_CONFIG_GLOBAL = $gitCfg
    Write-Host "  git URL redirect via GIT_CONFIG_GLOBAL=$gitCfg"
    Write-Host "    $RepoUrlHttps -> $fileUrl"
}

function Read-State {
    if (-not (Test-Path -LiteralPath $StatePath)) {
        throw "State file not found: $StatePath -- run '-Phase stage' first."
    }
    return Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
}

function Get-InstalledHead {
    return Invoke-Git @("-C", $InstallDir, "rev-parse", "HEAD")
}

function Get-DesktopExe {
    foreach ($c in @(
        (Join-Path $InstallDir "apps\desktop\release\win-unpacked\Hermes.exe"),
        (Join-Path $InstallDir "apps\desktop\release\win-arm64-unpacked\Hermes.exe")
    )) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    return $null
}

function Test-HermesRuns([string]$Label) {
    $hermesExe = Join-Path $InstallDir "venv\Scripts\hermes.exe"
    Assert-True (Test-Path -LiteralPath $hermesExe) "$Label -- venv\Scripts\hermes.exe exists"
    & $hermesExe --version 2>&1 | ForEach-Object { Write-Host "    hermes --version| $_" }
    Assert-True ($LASTEXITCODE -eq 0) "$Label -- hermes --version exits 0"
}

function Save-DesktopScreenshot([string]$OutFile) {
    # Single full-desktop screenshot (primary screen).
    try {
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
        $bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
        $gfx = [System.Drawing.Graphics]::FromImage($bmp)
        $gfx.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
        $bmp.Save($OutFile, [System.Drawing.Imaging.ImageFormat]::Png)
        $gfx.Dispose(); $bmp.Dispose()
        Write-Host "  desktop screenshot: $OutFile"
    } catch {
        Write-Host "  WARNING: desktop screenshot failed: $($_.Exception.Message)"
    }
}

function Start-ScreenRecording([string]$OutFile) {
    # Continuous ffmpeg screen capture (gdigrab, 15fps). ffmpeg ships on the
    # windows-latest runner image; skip gracefully elsewhere. mkv on purpose:
    # it stays playable even if the process dies without finalizing.
    #
    # ffmpeg must be started, fed, and stopped from THIS process: the
    # graceful stop is the character 'q' on its LIVE stdin pipe, which only
    # System.Diagnostics.Process exposes (Start-Process
    # -RedirectStandardInput hands it a file handle already at EOF).
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
        Write-Host "  (ffmpeg not on PATH; skipping screen recording)"
        return $null
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "ffmpeg"
    $psi.Arguments = "-y -f gdigrab -framerate 15 -i desktop " +
        "-hide_banner -loglevel error " +
        "-c:v libx264 -preset ultrafast -pix_fmt yuv420p `"$OutFile`""
    $psi.RedirectStandardInput = $true
    $psi.UseShellExecute = $false
    $proc = [System.Diagnostics.Process]::Start($psi)
    Write-Host "  screen recording started (pid $($proc.Id)) -> $OutFile"
    return $proc
}

function Stop-ScreenRecording($proc) {
    if ($proc -and -not $proc.HasExited) {
        try {
            $proc.StandardInput.Write("q")
            $proc.StandardInput.Close()
        } catch {}
        if (-not $proc.WaitForExit(15000)) { try { $proc.Kill() } catch {} }
    }
}

function Start-DesktopRecorder([string]$OutDir) {
    # Rolling desktop capture: one PNG every 3s from a detached PowerShell,
    # capped at 800 frames (~40 min). Proof that survives any step failure.
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    $script = Join-Path $WorkRoot "recorder.ps1"
    @'
param([string]$OutDir)
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
for ($i = 0; $i -lt 800; $i++) {
    if (Test-Path (Join-Path $OutDir "STOP")) { break }
    try {
        $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
        $bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
        $gfx = [System.Drawing.Graphics]::FromImage($bmp)
        $gfx.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
        $bmp.Save((Join-Path $OutDir ("frame-{0:D4}.png" -f $i)), [System.Drawing.Imaging.ImageFormat]::Png)
        $gfx.Dispose(); $bmp.Dispose()
    } catch {}
    Start-Sleep -Seconds 3
}
'@ | Set-Content -LiteralPath $script -Encoding UTF8
    $proc = Start-Process -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $script, "-OutDir", $OutDir `
        -WindowStyle Hidden -PassThru
    Write-Host "  desktop recorder started (pid $($proc.Id)) -> $OutDir"
    return $proc
}

function Stop-DesktopRecorder($proc, [string]$OutDir) {
    try { Set-Content -LiteralPath (Join-Path $OutDir "STOP") -Value "stop" } catch {}
    if ($proc) {
        try { $proc.WaitForExit(8000) | Out-Null } catch {}
        try { if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force } } catch {}
    }
}

function Stop-HermesAppProcesses([string]$Label) {
    # Close the desktop app the blunt way between phases (a user quitting).
    # Only Hermes.exe (Electron) -- never hermes.exe (the venv CLI shim).
    $procs = @(Get-Process -Name "Hermes" -ErrorAction SilentlyContinue)
    foreach ($p in $procs) {
        try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
    }
    if ($procs.Count -gt 0) {
        Write-Host "  [$Label] stopped $($procs.Count) Hermes.exe process(es)"
        Start-Sleep -Seconds 3
    }
}

function Get-ManagedNode {
    # `hermes update`/desktop builds use the Hermes-managed Node; use the same
    # one to run the Playwright driver so no system Node is required.
    $candidates = @(
        (Join-Path $HermesHome "node\node.exe"),
        (Join-Path $HermesHome "bin\node\node.exe"),
        (Join-Path $InstallDir "node\node.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    $fromPath = Get-Command node -ErrorAction SilentlyContinue
    if ($fromPath) { return $fromPath.Source }
    throw "No node.exe found (managed or on PATH)"
}

# ----------------------------------------------------------------------------
# Phase: stage -- serve.git with `main` at OLD (advanced to HEAD by update-gui)
# ----------------------------------------------------------------------------
function Invoke-PhaseStage {
    Write-Step "STAGE: bare serve repo, main -> OLD (install base)"

    if (Test-Path -LiteralPath $WorkRoot) {
        Remove-Item -LiteralPath $WorkRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $WorkRoot -Force | Out-Null
    # The purge above deleted the redirect gitconfig; re-arm it so the
    # bare-clone below (and everything after) sees the redirect file.
    Set-GitRedirect

    $current = Invoke-Git @("-C", $RepoRoot, "rev-parse", "HEAD")
    Write-Host "  HEAD (update target): $current"

    # OLD: explicit -InstallRef, or the newest release tag -- the version a
    # user who installed on release day is on.
    $oldRef = $InstallRef
    if (-not $oldRef -or $oldRef -eq "auto") {
        # Parens matter: without them PowerShell binds -split as an
        # argument to Invoke-Git instead of an operator on its result.
        $tagList = Invoke-Git @("-C", $RepoRoot, "tag", "--list", "v*", "--sort=-creatordate")
        $oldRef = ($tagList -split "\r?\n" | Select-Object -First 1)
        if (-not $oldRef) { throw "no v* release tags in the checkout and no -InstallRef given -- cannot pick an OLD version" }
    }
    $old = Invoke-Git @("-C", $RepoRoot, "rev-parse", "$oldRef^{commit}")
    Write-Host "  OLD  ($oldRef): $old"
    Assert-True ($old -ne $current) "OLD differs from HEAD (an update is genuinely available)"

    # Bare-clone the checkout: this is the repo the installer and updater
    # actually talk to. Local-path clone hardlinks objects, so it's fast
    # even for full history. The published installer carries NO commit pin
    # (Pin { commit: None, branch: main }) -- it installs whatever `main`
    # serves, so staging OLD means parking `main` there; the update phase
    # advances it to HEAD.
    Invoke-Git @("clone", "--bare", "--quiet", $RepoRoot, $ServeRepo) | Out-Null
    Invoke-Git @("-C", $ServeRepo, "update-ref", "refs/heads/main", $old) | Out-Null
    Invoke-Git @("-C", $ServeRepo, "symbolic-ref", "HEAD", "refs/heads/main") | Out-Null

    # Belt-and-braces: SOME installer builds do bake a -Commit pin. A pinned
    # sha is in serve.git's history but not at a ref tip, so the redirected
    # fetch needs any-SHA1 upload-pack permission (GitHub grants the
    # equivalent for fetch of reachable commits).
    Invoke-Git @("-C", $ServeRepo, "config", "uploadpack.allowAnySHA1InWant", "true") | Out-Null
    Write-Host "  serve.git: uploadpack.allowAnySHA1InWant=true (installer commit pin, if any)"

    @{ old = $old; old_ref = $oldRef; current = $current } |
        ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
    Write-Host "  state written: $StatePath"
    New-Item -ItemType Directory -Path $ProofRoot -Force | Out-Null
}

# ----------------------------------------------------------------------------
# Phase: install-gui -- website Hermes-Setup.exe, headed, AHK-driven
# ----------------------------------------------------------------------------
function Invoke-PhaseInstallGui {
    $state = Read-State
    Write-Step "INSTALL (GUI): Hermes-Setup.exe from the website, headed, AHK clicks"
    $proof = Join-Path $ProofRoot "install-gui"
    New-Item -ItemType Directory -Path $proof -Force | Out-Null

    # The production installer, from the website. This is the binary users
    # double-click, run EXACTLY as shipped: its own pinned install.ps1, its
    # own baked BUILD_PIN_COMMIT. The only environmental difference is the
    # git URL redirect to serve.git.
    $setupExe = Join-Path $WorkRoot "Hermes-Setup.exe"
    if (-not (Test-Path -LiteralPath $setupExe)) {
        Write-Host "  downloading $SetupExeUrl"
        Invoke-WebRequest -Uri $SetupExeUrl -OutFile $setupExe
    }
    Assert-True ((Get-Item $setupExe).Length -gt 1MB) "Hermes-Setup.exe downloaded ($([math]::Round((Get-Item $setupExe).Length / 1MB, 1)) MB)"

    # AutoHotkey v2, portable zip (no installer, no winget flakes).
    $ahkExe = Join-Path $AhkDir "AutoHotkey64.exe"
    if (-not (Test-Path -LiteralPath $ahkExe)) {
        $zip = Join-Path $WorkRoot "ahk.zip"
        Invoke-WebRequest -Uri "https://github.com/AutoHotkey/AutoHotkey/releases/download/v2.0.19/AutoHotkey_2.0.19.zip" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $AhkDir -Force
    }
    Assert-True (Test-Path -LiteralPath $ahkExe) "AutoHotkey64.exe available"

    # AHK script + button templates side by side (ImageSearch resolves
    # relative to the script dir).
    Copy-Item -Path (Join-Path $AssetsDir "install-and-launch.ahk"), (Join-Path $AssetsDir "install-button.png"), (Join-Path $AssetsDir "launch-button.png") -Destination $AhkDir -Force

    $env:HERMES_HOME = $HermesHome
    # As shipped: NO dev-root override, no pin override. Ensure a stray
    # local dev checkout can't hijack resolution.
    Remove-Item Env:HERMES_SETUP_DEV_REPO_ROOT -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $HermesHome -Force | Out-Null

    $recorder = Start-DesktopRecorder (Join-Path $proof "desktop-frames")
    $recording = Start-ScreenRecording (Join-Path $proof "recording.mkv")
    $ahkLog = Join-Path $proof "ahk.log"
    try {
        Save-DesktopScreenshot (Join-Path $proof "00-before-installer.png")

        # Launch the REAL installer, headed -- exactly a double-click.
        $installer = Start-Process -FilePath $setupExe -PassThru
        Write-Host "  Hermes-Setup.exe launched (pid $($installer.Id))"

        # Drive it: Install click -> wait -> Launch click -> Hermes.exe window.
        # Arg 3 lets the AHK script use the installer's own log as the
        # install-finished fallback signal.
        $ahk = Start-Process -FilePath $ahkExe `
            -ArgumentList (Join-Path $AhkDir "install-and-launch.ahk"), $ahkLog, "Hermes-Setup.exe", (Join-Path $HermesHome "logs\bootstrap-installer.log") `
            -PassThru
        # Install on a cold runner takes a while; the AHK script's own inner
        # timeout (45 min on the Launch wait) is the effective budget.
        if (-not $ahk.WaitForExit(50 * 60 * 1000)) {
            Stop-Process -Id $ahk.Id -Force -ErrorAction SilentlyContinue
            throw "AutoHotkey driver did not finish within 50 minutes"
        }
        if (Test-Path -LiteralPath $ahkLog) {
            Get-Content -LiteralPath $ahkLog | ForEach-Object { Write-Host "  ahk| $_" }
        }
        Assert-True ($ahk.ExitCode -eq 0) "AutoHotkey driver exited 0 (Install clicked, Launch clicked, app window seen)"

        Save-DesktopScreenshot (Join-Path $proof "01-app-launched.png")

        # The Launch hand-off under test: the app the installer spawned must
        # actually be running.
        Assert-True ($null -ne (Get-Process -Name "Hermes" -ErrorAction SilentlyContinue)) "Hermes.exe process is running (installer Launch hand-off worked)"

        # Installer should have exited after Launch.
        if (-not $installer.HasExited) {
            Start-Sleep -Seconds 10
        }
        Assert-True $installer.HasExited "Hermes-Setup.exe exited after Launch"
    }
    finally {
        Stop-ScreenRecording $recording
        Stop-DesktopRecorder $recorder (Join-Path $proof "desktop-frames")
        # Surface the installer's own log win or lose.
        $bootLog = Join-Path $HermesHome "logs\bootstrap-installer.log"
        if (Test-Path -LiteralPath $bootLog) {
            Write-Host "  --- bootstrap-installer.log (tail) ---"
            Get-Content -LiteralPath $bootLog -Tail 40 | ForEach-Object { Write-Host "  | $_" }
            Copy-Item $bootLog $proof -Force -ErrorAction SilentlyContinue
        }
    }

    # Close the freshly launched app (user quits after first look).
    Stop-HermesAppProcesses "post-install"

    # The installer cloned serve.git's `main`, which stage parked at OLD.
    # (A pinned installer build would land on its baked pin instead; either
    # way the requirement is the same: not already on HEAD.)
    $installedSha = Get-InstalledHead
    Write-Host "  installer landed on: $installedSha (OLD = $($state.old) [$($state.old_ref)])"
    Assert-True ($installedSha -eq $state.old) "installed checkout is at OLD ($($state.old_ref))"
    Assert-True ($installedSha -ne $state.current) "installed checkout differs from HEAD (an update is genuinely available)"
    Test-HermesRuns "post-install-gui"
    Assert-True ($null -ne (Get-DesktopExe)) "packaged Desktop Hermes.exe exists"

    # Seed a provider so the update leg meets the ready app shell, not the
    # onboarding overlay (an updating user has a configured provider).
    $envFile = Join-Path $HermesHome ".env"
    if (-not (Test-Path -LiteralPath $envFile) -or -not ((Get-Content $envFile -Raw -ErrorAction SilentlyContinue) -match "OPENROUTER_API_KEY")) {
        Add-Content -LiteralPath $envFile -Value "OPENROUTER_API_KEY=sk-or-...-key"
    }
    Write-Host "  seeded placeholder provider key for the update leg"

    # Suppress the interactive "add upstream remote?" prompt during the GUI
    # update leg. Our serve.git origin (file://) looks like a fork to
    # `hermes update`, so `_sync_with_upstream_if_needed` would call bare
    # input() -- which HANGS FOREVER when the Desktop spawns the hand-off via
    # `cmd start /min` (a real but empty console; input() blocks waiting for a
    # keystroke that never comes). Real GUI users on the official github
    # origin never hit this path (_is_fork is false). The skip marker
    # (.skip_upstream_prompt in HERMES_HOME) is the product's own mechanism
    # for "don't ask about upstream", so setting it keeps the update flow
    # faithful while avoiding the fork-only prompt.
    New-Item -ItemType File -Path (Join-Path $HermesHome ".skip_upstream_prompt") -Force | Out-Null
    Write-Host "  set .skip_upstream_prompt (serve.git origin looks like a fork; avoids the fork-only input() hang)"
}

# ----------------------------------------------------------------------------
# Phase: update-gui -- OLD -> HEAD through the selected route
# ----------------------------------------------------------------------------
function Invoke-GuiUpdateDesktopRoute([string]$TargetSha) {
    Write-Step "UPDATE (GUI, route=desktop): advance served main -> $TargetSha, click Update now"
    $proof = Join-Path $ProofRoot "update-gui"
    New-Item -ItemType Directory -Path $proof -Force | Out-Null

    $env:HERMES_HOME = $HermesHome

    # The update becomes available the way it does for a real user: the
    # remote's main moves forward. (Install ran against main = OLD.)
    Invoke-Git @("-C", $ServeRepo, "update-ref", "refs/heads/main", $TargetSha) | Out-Null
    Write-Host "  serve.git main advanced to $TargetSha"

    $desktopExe = Get-DesktopExe
    Assert-True ($null -ne $desktopExe) "packaged Hermes.exe present before update"

    $resultPath = Join-Path $HermesHome ".hermes-update-result.json"
    $markerPath = Join-Path $HermesHome ".hermes-update-in-progress"
    Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue

    $node = Get-ManagedNode
    $appsDesktop = Join-Path $InstallDir "apps\desktop"
    # @playwright/test is a workspace devDependency; the root `npm ci`
    # HOISTS it to the repo-root node_modules, not apps/desktop's. Resolve
    # it the way Node will (walk up from apps/desktop) instead of asserting
    # a hardcoded path that hoisting makes wrong.
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    & $node -e "require.resolve('@playwright/test', { paths: [process.argv[1]] })" $appsDesktop 2>&1 | Out-Null
    $pwResolved = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prevEap
    Assert-True $pwResolved "@playwright/test resolvable from installed apps/desktop"

    $recorder = Start-DesktopRecorder (Join-Path $proof "desktop-frames")
    $recording = Start-ScreenRecording (Join-Path $proof "recording.mkv")
    try {
        # Launch the installed app and click through Settings -> About ->
        # Update now. Exit 0 = the app quit for the updater hand-off.
        # Copy the driver INTO the installed apps/desktop first: Node resolves
        # require('@playwright/test') from the SCRIPT's own directory upward,
        # so running it from the CI checkout would resolve the wrong (or no)
        # node_modules.
        $driver = Join-Path $appsDesktop "e2e-drive-update.cjs"
        Copy-Item (Join-Path $AssetsDir "drive-update.cjs") $driver -Force
        Push-Location $appsDesktop
        try {
            & $node $driver $desktopExe $proof 2>&1 |
                ForEach-Object { Write-Host "  $_" }
            $driveExit = $LASTEXITCODE
        } finally {
            Pop-Location
            Remove-Item -LiteralPath $driver -Force -ErrorAction SilentlyContinue
        }
        Assert-True ($driveExit -eq 0) "GUI driver clicked Update now and the app quit for hand-off"

        # The detached updater (spawned by the app, NOT by us) now runs
        # `hermes update` + desktop rebuild + relaunch. Which updater depends
        # on the installed checkout, and BOTH are production paths:
        #   * checkouts shipping scripts/desktop-update.ps1 -> that script,
        #     which writes .hermes-update-result.json on every exit;
        #   * older checkouts -> the staged hermes-setup.exe --update flow,
        #     which does NOT write the result JSON.
        # So: poll for COMPLETION = (result JSON) OR (checkout reached the
        # target sha AND the marker is gone). The sha/marker/hermes/relaunch
        # asserts below are the hard gate either way; the JSON is asserted
        # only when the script path produced it.
        #
        # This can take a LONG time: the website release we installed is weeks
        # of main behind HEAD, so the update pulls a large diff AND does a
        # full Electron desktop rebuild (vite + electron-builder) plus a uv
        # sync. The desktop-build output goes to logs/update.log (not the
        # streamed handoff log), so we tail update.log here to show progress
        # instead of going silent for tens of minutes.
        Write-Host "  waiting for the detached updater to finish (up to 90 min; large old->new rebuild) ..."
        $updateLog = Join-Path $HermesHome "logs\update.log"
        $updateLogPos = 0
        $deadline = (Get-Date).AddMinutes(90)
        while ((Get-Date) -lt $deadline) {
            if (Test-Path -LiteralPath $resultPath) { break }
            $head = ""
            try { $head = Get-InstalledHead } catch {}
            if ($head -eq $TargetSha -and -not (Test-Path -LiteralPath $markerPath)) { break }
            # Tail any new update.log lines so the desktop-rebuild phase is
            # visible in the CI step output.
            if (Test-Path -LiteralPath $updateLog) {
                try {
                    $lines = Get-Content -LiteralPath $updateLog -ErrorAction SilentlyContinue
                    if ($lines.Count -gt $updateLogPos) {
                        $lines[$updateLogPos..($lines.Count - 1)] | ForEach-Object { Write-Host "    update.log| $_" }
                        $updateLogPos = $lines.Count
                    }
                } catch {}
            }
            Start-Sleep -Seconds 20
        }
        if (Test-Path -LiteralPath $resultPath) {
            $result = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
            Write-Host "  updater result: ok=$($result.ok) code=$($result.exit_code) msg=$($result.message)"
            Assert-True ([bool]$result.ok) "updater result ok=true"
        } else {
            Write-Host "  (no result JSON -- staged-binary updater path; relying on sha/marker/relaunch asserts)"
        }

        # Marker may briefly outlive the result write; allow it a moment.
        $mDeadline = (Get-Date).AddMinutes(2)
        while ((Get-Date) -lt $mDeadline -and (Test-Path -LiteralPath $markerPath)) { Start-Sleep -Seconds 5 }
        Assert-True (-not (Test-Path -LiteralPath $markerPath)) "update marker cleaned up"

        Assert-True ((Get-InstalledHead) -eq $TargetSha) "checkout landed on target commit"
        Test-HermesRuns "post-update"
        Assert-True ($null -ne (Get-DesktopExe)) "Hermes.exe still present after update"

        # The production hand-off relaunches the desktop (RelaunchExe).
        # A relaunched window is the user-visible proof the update loop closed.
        Write-Host "  waiting for the relaunched Hermes.exe ..."
        $rDeadline = (Get-Date).AddMinutes(5)
        $relaunched = $null
        while ((Get-Date) -lt $rDeadline) {
            $relaunched = Get-Process -Name "Hermes" -ErrorAction SilentlyContinue
            if ($relaunched) { break }
            Start-Sleep -Seconds 5
        }
        Assert-True ($null -ne $relaunched) "updater relaunched the desktop app"
        Start-Sleep -Seconds 12   # let the window paint for the screenshot
        # Foreground the relaunched Hermes window so the proof screenshot
        # captures IT, not whatever else is on top (the full-desktop grab is
        # otherwise at the mercy of z-order -- an earlier run caught VS Code).
        try {
            $mainProc = Get-Process -Name "Hermes" -ErrorAction SilentlyContinue |
                Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
            if ($mainProc) {
                Add-Type -Namespace HdE2E -Name Win -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int n);
'@ -ErrorAction SilentlyContinue
                [HdE2E.Win]::ShowWindow($mainProc.MainWindowHandle, 9) | Out-Null   # SW_RESTORE
                [HdE2E.Win]::SetForegroundWindow($mainProc.MainWindowHandle) | Out-Null
                Start-Sleep -Seconds 2
            }
        } catch {}
        Save-DesktopScreenshot (Join-Path $proof "99-relaunched-desktop.png")
    }
    finally {
        Stop-ScreenRecording $recording
        Stop-DesktopRecorder $recorder (Join-Path $proof "desktop-frames")
        $handoffLog = Join-Path $HermesHome "logs\desktop-update-handoff.log"
        if (Test-Path -LiteralPath $handoffLog) {
            Write-Host "  --- desktop-update-handoff.log (tail) ---"
            Get-Content -LiteralPath $handoffLog -Tail 60 | ForEach-Object { Write-Host "  | $_" }
            Copy-Item $handoffLog (Join-Path $proof "desktop-update-handoff.log") -Force -ErrorAction SilentlyContinue
        }
        # Quit the relaunched app so job teardown is clean.
        Stop-HermesAppProcesses "post-update"
    }
}

function Invoke-PhaseUpdateGui {
    $state = Read-State
    switch ($Route) {
        "desktop-app" {
            Invoke-GuiUpdateDesktopRoute $state.current
        }
        "hermes-update" {
            # TODO: run `hermes update` from the installed venv -- the CLI
            # route. Needs the same completion/sha asserts minus the
            # app-quit dance.
            throw "update method 'hermes-update' is not implemented yet"
        }
        "desktop-installer-rerun@latest" {
            # TODO: re-run the bootstrap Hermes-Setup.exe over the existing
            # install (its --update flow jumps straight to progress and
            # runs unattended).
            throw "update method 'desktop-installer-rerun@latest' is not implemented yet"
        }
        "irm-iex" {
            # TODO: re-run the irm | iex one-liner over the existing
            # install.
            throw "update method 'irm-iex' is not implemented yet"
        }
    }
}

# ----------------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------------
Write-Host "Windows Desktop GUI E2E driver (real user flow)"
Write-Host "  phase:    $Phase"
Write-Host "  route:    $Route"
Write-Host "  repo:     $RepoRoot"
Write-Host "  workroot: $WorkRoot"

Set-GitRedirect

switch ($Phase) {
    "stage"       { Invoke-PhaseStage }
    "install-gui" { Invoke-PhaseInstallGui }
    "update-gui"  { Invoke-PhaseUpdateGui }
    "all" {
        Invoke-PhaseStage
        Invoke-PhaseInstallGui
        Invoke-PhaseUpdateGui
    }
}

Write-Host ""
Write-Host "Phase '$Phase' completed successfully."
