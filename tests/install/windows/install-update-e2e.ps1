# Prove a Windows desktop user on the published bootstrap installer can reach
# this commit through the desktop's builtin update.
#
# The Windows analog of tests/install/install-update-e2e.sh. Linux gets its
# fake GitHub from the bubblewrap sandbox's MITM proxy + git-upload-pack shim;
# there is no such sandbox on Windows, so the git proxying here is git's own
# transport rewrite instead: a throwaway GIT_CONFIG_GLOBAL carrying multi-
# valued url.<file://fake.git>.insteadOf entries for BOTH hardcoded repo URL
# forms (SSH and HTTPS). Every git child of this process -- install.ps1's
# clone, `hermes update`'s fetch, the desktop's ls-remote -- resolves
# github.com/NousResearch/hermes-agent to a local bare repo whose `main` this
# driver controls, while the tools themselves run VERBATIM with their real
# URLs. scripts/fake_remote_update_probe.sh (hermes-install-update-testing
# skill) is the verified reference for the mechanism, including why --add is
# load-bearing (a second plain `git config` REPLACES the first rewrite and one
# URL silently reaches real GitHub).
#
# What one run does:
#   1. Seed fake.git from this checkout (all origin branches + tags), then
#      force fake `main` to the NEWEST release tag -- so an installer with a
#      branch pin lands on a released base, not on the target.
#      uploadpack.allowAnySHA1InWant covers installers with a -Commit pin.
#   2. Run the real published Hermes-Setup.exe, driven by AutoHotkey (the
#      installer is a GUI with no headless mode). The exe downloads its
#      pinned install.ps1 from raw.githubusercontent for real -- the same
#      fixture-miss passthrough posture as the Linux sandbox -- and that
#      script's git clone rides the rewrite onto fake.git.
#   3. Promote fake `main` to this checkout's HEAD (the --from-main dance).
#   4. Apply ONE update route and require HEAD == target with a working
#      `hermes`.
#
# Routes:
#   desktop    the desktop app's builtin update, minus only the Electron
#              process around it, following applyUpdates' own preference
#              order (apps/desktop/electron/main.ts): the repo-owned
#              scripts/desktop-update.ps1 hand-off when the installed base
#              ships it, else the staged hermes-setup.exe --update (which
#              auto-runs: update mode is a hand-off, not a click-through).
#              Either way the update engine is the installed release's own
#              `hermes update`, so old CLIs meet their contemporaneous flags.
#   TODO update      bare `hermes update` from the installed venv (the route
#                    the Linux matrix calls `update`).
#   TODO installer   re-run the bootstrap installer over the existing
#                    checkout (the Linux `installer` route; needs the AHK
#                    flow to handle the repair/reinstall UI).
#
# Requires: git, AutoHotkey64.exe on PATH, network (real toolchain download),
# a clean full-history checkout with release tags fetched. ffmpeg on PATH is
# optional -- when present the run is screen-recorded for the artifact.

#Requires -Version 7

param(
    [ValidateSet("desktop")]
    [string]$Route = "desktop",
    # Latest published installer -- "what a user downloads today".
    [string]$InstallerUrl = "https://hermes-assets.nousresearch.com/Hermes-Setup.exe",
    # Local exe override (skips the download; for iterating on this driver).
    [string]$InstallerPath = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$RepoUrlSsh = "git@github.com:NousResearch/hermes-agent.git"
$RepoUrlHttps = "https://github.com/NousResearch/hermes-agent.git"

# Everything lives OUTSIDE the checkout: an untracked dir inside the repo
# would make later verification steps lie about a dirty tree, and RUNNER_TEMP
# is wiped with the runner.
$WorkRoot = Join-Path ($env:RUNNER_TEMP ?? [System.IO.Path]::GetTempPath()) "hermes-install-e2e"
$LogDir = if ($env:HERMES_E2E_LOG_DIR) { $env:HERMES_E2E_LOG_DIR } else { Join-Path $WorkRoot "logs" }
$FakeRepo = Join-Path $WorkRoot "fake.git"

function Step([string]$Message) { Write-Host "`n=== $Message ===" }
function Ok([string]$Message) { Write-Host "  OK $Message" }
function Fail([string]$Message) {
    Write-Host "FAIL: $Message" -ForegroundColor Red
    exit 1
}

function Invoke-Git {
    param([string[]]$GitArgs, [string]$Cwd = $RepoRoot)
    $out = & git -C $Cwd @GitArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        Fail "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE): $out"
    }
    return ($out | Out-String).Trim()
}

# --- preflight ---------------------------------------------------------------

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git not on PATH" }
if (-not (Get-Command AutoHotkey64.exe -ErrorAction SilentlyContinue)) {
    Fail "AutoHotkey64.exe not on PATH (winget install AutoHotkey.AutoHotkey)"
}

# The promote step pushes this worktree's HEAD as the update target, so a
# dirty tree means the tested commit is not the commit anyone can review.
$dirty = & git -C $RepoRoot status --porcelain
if ($dirty) {
    Write-Host ($dirty | Out-String)
    Fail "working tree is dirty; the update target must be a real commit"
}

Remove-Item -Recurse -Force $WorkRoot -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $WorkRoot, $LogDir | Out-Null

# Isolated HERMES_HOME so the real install never touches the runner's (or a
# developer's) profile. Both install.ps1 and the Tauri installer honor it.
if (-not $env:HERMES_HOME) {
    $env:HERMES_HOME = Join-Path $WorkRoot "hermes-home"
}
$InstallRoot = Join-Path $env:HERMES_HOME "hermes-agent"
$TargetSha = Invoke-Git @("rev-parse", "HEAD")

# Pre-seed the managed uv. install.ps1's Install-Uv runs astral's installer
# with UV_INSTALL_DIR pointing into HERMES_HOME\bin and discards its output --
# but when an astral install RECEIPT exists (GitHub runners ship uv
# preinstalled with one), the cargo-dist installer updates the receipt's
# location in place and ignores UV_INSTALL_DIR, so the managed path stays
# empty and the stage fails blind ("uv installed but not found", run
# 31447045981). Install-Uv short-circuits on an existing managed uv, so
# seeding it is a legitimate user state, not a bypass.
$managedBin = Join-Path $env:HERMES_HOME "bin"
New-Item -ItemType Directory -Force -Path $managedBin | Out-Null
$uvOnRunner = Get-Command uv.exe -ErrorAction SilentlyContinue
if ($uvOnRunner) {
    Copy-Item $uvOnRunner.Source (Join-Path $managedBin "uv.exe") -Force
    Ok "seeded managed uv from runner: $($uvOnRunner.Source)"
} else {
    # No preinstalled uv means no receipt, so the plain astral path works --
    # with output visible, unlike Install-Uv's.
    $env:UV_INSTALL_DIR = $managedBin
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    Remove-Item Env:\UV_INSTALL_DIR
    if (-not (Test-Path (Join-Path $managedBin "uv.exe"))) {
        Fail "could not seed managed uv into $managedBin"
    }
    Ok "seeded managed uv via astral installer"
}

# --- fake GitHub -------------------------------------------------------------

Step "seeding fake remote at $FakeRepo"
Invoke-Git @("init", "--bare", "--initial-branch=main", $FakeRepo) $WorkRoot | Out-Null
# Published installers carry a -Commit pin and fetch that raw SHA; a bare
# repo refuses SHA wants unless told otherwise.
Invoke-Git @("config", "uploadpack.allowAnySHA1InWant", "true") $FakeRepo | Out-Null

# All origin branches + tags: the installer's build pin may be any commit on
# any branch that existed when the exe was built.
Invoke-Git @("push", "--quiet", $FakeRepo, "refs/remotes/origin/*:refs/heads/*")
Invoke-Git @("push", "--quiet", "--force", $FakeRepo, "refs/tags/*:refs/tags/*")

# Fake main starts at the newest release tag: a released base a real user
# could be installed on, and never the update target itself. Major capped at
# three digits, matching _parse_release_tag (hermes_cli/update_cmd.py) and
# latestReleaseFromLsRemote (apps/desktop/electron/bundled-runtime.ts): the
# repo's historical CalVer tags (v2026.7.20) would otherwise win every
# numeric sort forever.
$releaseTags = @(& git -C $RepoRoot tag --list |
    Where-Object { $_ -match '^v\d{1,3}\.\d+\.\d+(\.\d+)?$' } |
    Sort-Object { [version]($_.Substring(1)) })
if ($releaseTags.Count -eq 0) {
    Fail "no release tags in this checkout -- fetch with tags (fetch-depth: 0 + fetch-tags)"
}
$newestTag = $releaseTags[-1]
$baseMainSha = Invoke-Git @("rev-parse", "$newestTag^{commit}")
Invoke-Git @("push", "--quiet", "--force", $FakeRepo, "${baseMainSha}:refs/heads/main")
Ok "fake main = $newestTag ($($baseMainSha.Substring(0,12))); target is $($TargetSha.Substring(0,12))"

# --- git transport rewrite ---------------------------------------------------

Step "redirecting github.com/NousResearch/hermes-agent to the fake remote"
# Process-scoped global config: every git spawned below this point (installer,
# hermes update, desktop hand-off) inherits it; nothing on the machine does.
$env:GIT_CONFIG_GLOBAL = Join-Path $WorkRoot "gitconfig"
Set-Content -Path $env:GIT_CONFIG_GLOBAL -Value "" -NoNewline
# Fail loudly if anything still reaches a URL that wants credentials.
$env:GIT_TERMINAL_PROMPT = "0"

$fakeUrl = "file:///" + $FakeRepo.Replace("\", "/")
foreach ($url in @($RepoUrlSsh, $RepoUrlHttps)) {
    Invoke-Git @("config", "--global", "--add", "url.$fakeUrl.insteadOf", $url) $WorkRoot
}
$rewrites = @(& git config --global --get-all "url.$fakeUrl.insteadOf")
if ($rewrites.Count -ne 2) {
    Fail "expected 2 insteadOf rewrites, got $($rewrites.Count) -- one URL would reach real GitHub"
}
Ok "both repo URL forms rewritten (SSH clone attempts ride the file transport)"

# --- fetch the installer -----------------------------------------------------

if (-not $InstallerPath) {
    Step "downloading published installer"
    $InstallerPath = Join-Path $WorkRoot "Hermes-Setup.exe"
    Invoke-WebRequest -Uri $InstallerUrl -OutFile $InstallerPath
}
Ok "installer: $InstallerPath ($([math]::Round((Get-Item $InstallerPath).Length / 1MB, 1)) MB)"

# --- screen recording (optional) ----------------------------------------------

# ffmpeg must be started, fed, and stopped from THIS process: the graceful
# stop is the character 'q' on its LIVE stdin pipe, which only
# System.Diagnostics.Process exposes (Start-Process -RedirectStandardInput
# hands it a file handle already at EOF).
$ffmpeg = $null
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "ffmpeg"
    $psi.Arguments = "-y -f gdigrab -framerate 15 -i desktop " +
        "-hide_banner -loglevel error " +
        "-c:v libx264 -preset ultrafast -pix_fmt yuv420p `"$LogDir\recording.mkv`""
    $psi.RedirectStandardInput = $true
    $psi.UseShellExecute = $false
    $ffmpeg = [System.Diagnostics.Process]::Start($psi)
    Ok "screen recording started (pid $($ffmpeg.Id))"
} else {
    Write-Host "  (ffmpeg not on PATH; skipping screen recording)"
}

function Stop-Recording {
    if ($script:ffmpeg -and -not $script:ffmpeg.HasExited) {
        try {
            $script:ffmpeg.StandardInput.Write("q")
            $script:ffmpeg.StandardInput.Close()
        } catch {}
        if (-not $script:ffmpeg.WaitForExit(15000)) { $script:ffmpeg.Kill() }
    }
}

# --- run the real installer under AutoHotkey ----------------------------------

Step "installing via Hermes-Setup.exe (real toolchains: git, uv, Python, Node, venv, desktop)"
$installerOk = $false
try {
    $proc = Start-Process -FilePath $InstallerPath -PassThru

    # Lossless PNG of the welcome screen, taken BEFORE the AHK helper starts
    # so no tooltip or click marker contaminates it. This is the artifact the
    # ImageSearch reference crop is made from: the ffmpeg recording is
    # H.264/yuv420p, whose chroma subsampling shifts glyph pixels enough that
    # a crop from video never matches the live screen.
    #
    # Poll instead of a fixed sleep: WebView2's cold start left the window
    # pure white past 17s on a runner (run 31448964917's capture was blank).
    # "Rendered" = colored (non-grayscale) pixels in the window content area,
    # which the blue HERMES AGENT title guarantees.
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $shotPath = Join-Path $LogDir "welcome-screen.png"
    $renderDeadline = (Get-Date).AddSeconds(120)
    $rendered = $false
    while ((Get-Date) -lt $renderDeadline) {
        Start-Sleep -Seconds 5
        $bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
        $gfx = [System.Drawing.Graphics]::FromImage($bmp)
        $gfx.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
        $gfx.Dispose()
        $colored = 0
        for ($y = 100; $y -lt 600; $y += 7) {
            for ($x = 100; $x -lt 900; $x += 7) {
                $p = $bmp.GetPixel($x, $y)
                $mx = [Math]::Max($p.R, [Math]::Max($p.G, $p.B))
                $mn = [Math]::Min($p.R, [Math]::Min($p.G, $p.B))
                if (($mx - $mn) -gt 60) { $colored++ }
            }
        }
        $bmp.Save($shotPath, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
        Write-Host "  screen poll: $colored colored samples"
        if ($colored -gt 20) { $rendered = $true; break }
    }
    if (-not $rendered) {
        Stop-Recording
        Fail "installer UI never rendered within 120s (last capture saved to welcome-screen.png)"
    }
    Ok "welcome screen captured (lossless) to welcome-screen.png"

    # TEMPORARY: stop here. The ImageSearch reference crop has to be cut from
    # this capture before the AHK click path can work, so running it now only
    # burns a doomed 30-minute leg. Remove this exit once install-button.png
    # is regenerated from welcome-screen.png.
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Stop-Recording
    Write-Host "STOPPING EARLY: welcome-screen.png captured; AHK path disabled until the button crop is regenerated from it."
    exit 0

    $ahkLog = Join-Path $LogDir "ahk.log"
    # The helper polls for the installer's own completion signal instead of a
    # second button screenshot (see paths.rs likely_bootstrap_marker).
    $bootstrapMarker = Join-Path $InstallRoot ".hermes-bootstrap-complete"
    # -NoNewWindow attaches our console as the GUI-subsystem exe's stdout so
    # the helper's live lines land in the job log as they happen.
    $ahkProc = Start-Process -FilePath "AutoHotkey64.exe" -NoNewWindow `
        -ArgumentList "`"$PSScriptRoot\install-hermes-desktop.ahk`"", "`"$ahkLog`"", "`"$bootstrapMarker`"" -PassThru

    # Tail the bootstrap log into the job log while we wait: the install IS
    # the substance of this test, and a failure explanation should not need
    # an artifact download. FileShare.ReadWrite because the installer still
    # has the file open for writing.
    $logReader = $null
    $logStream = $null
    $bootstrapLog = Join-Path $env:HERMES_HOME "logs\bootstrap-installer.log"
    $deadline = (Get-Date).AddMinutes(30)
    try {
        while ((Get-Date) -lt $deadline -and -not $ahkProc.HasExited) {
            if (-not $logReader) {
                if (Test-Path $bootstrapLog) {
                    $logStream = [System.IO.File]::Open($bootstrapLog, 'Open', 'Read', 'ReadWrite')
                    $logReader = New-Object System.IO.StreamReader($logStream)
                }
            } else {
                $line = $logReader.ReadLine()
                while ($null -ne $line) {
                    Write-Host "[bootstrap] $line"
                    # The installer's failure screen waits for a human (Retry
                    # button); the AHK helper would idle out its full marker
                    # deadline. Abort as soon as the log says the run is dead.
                    if ($line -match "bootstrap FAILED") {
                        Stop-Process -Id $ahkProc.Id -Force -ErrorAction SilentlyContinue
                        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                        Fail "installer reported: $line"
                    }
                    $line = $logReader.ReadLine()
                }
            }
            Start-Sleep -Milliseconds 500
        }
        # Drain what was written in the final tick.
        if ($logReader) {
            $line = $logReader.ReadLine()
            while ($null -ne $line) {
                Write-Host "[bootstrap] $line"
                $line = $logReader.ReadLine()
            }
        }
    } finally {
        if ($logReader) { $logReader.Dispose() }
        if ($logStream) { $logStream.Dispose() }
    }

    if (-not $ahkProc.HasExited) {
        Stop-Process -Id $ahkProc.Id -Force -ErrorAction SilentlyContinue
        Fail "AutoHotkey helper still running at the deadline -- install never finished. See ahk.log + recording."
    }
    if ($ahkProc.ExitCode -ne 0) {
        Fail "AutoHotkey helper failed (exit $($ahkProc.ExitCode)) -- see ahk.log + recording"
    }

    # The AHK helper closes the window after the Launch button appears; a
    # still-running installer means the close did not land.
    if (-not $proc.WaitForExit(30000)) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Fail "installer process still running after the window was closed"
    }
    $installerOk = $true
} finally {
    Stop-Recording
    if (Test-Path (Join-Path $LogDir "ahk.log")) {
        Write-Host "--- ahk.log ---"
        Get-Content (Join-Path $LogDir "ahk.log") | ForEach-Object { Write-Host $_ }
        Write-Host "--- end ahk.log ---"
    }
    if (-not $installerOk -and (Test-Path (Join-Path $env:HERMES_HOME "logs\bootstrap-installer.log"))) {
        Copy-Item (Join-Path $env:HERMES_HOME "logs\bootstrap-installer.log") $LogDir -Force
    }
}

# --- verify the install ------------------------------------------------------

Step "verifying the installed checkout"
if (-not (Test-Path (Join-Path $InstallRoot ".git"))) {
    Fail "no git checkout at $InstallRoot -- the installer's clone did not ride the rewrite?"
}
$BaseSha = Invoke-Git @("rev-parse", "HEAD") $InstallRoot
if ($BaseSha -eq $TargetSha) {
    Fail "install landed on the update target ($BaseSha); base and target must differ"
}
Ok "installed $($BaseSha.Substring(0,12)); update target is $($TargetSha.Substring(0,12))"

$HermesExe = Join-Path $InstallRoot "venv\Scripts\hermes.exe"
if (-not (Test-Path $HermesExe)) { Fail "venv shim missing: $HermesExe" }

# The real smoke test: goes through the venv launcher and imports the app.
$version = & $HermesExe --version 2>&1
if ($LASTEXITCODE -ne 0) { Fail "hermes --version failed after install: $version" }
Write-Host "    $version"
Ok "hermes runs after install"

# --- promote fake main to this checkout --------------------------------------

Step "promoting fake main to this checkout (the state a user sees when an update is waiting)"
Invoke-Git @("push", "--quiet", "--force", $FakeRepo, "HEAD:refs/heads/main")
Ok "fake main advanced to $($TargetSha.Substring(0,12))"

# --- apply exactly one update route -------------------------------------------

switch ($Route) {
    "desktop" {
        Step "ROUTE: desktop builtin update"
        # Mirror the PATH contract the desktop passes the hand-off
        # (pathWithHermesManagedNode in apps/desktop/electron/main.ts):
        # managed node first, then the venv scripts dir.
        $managedNode = Join-Path $env:HERMES_HOME "node"
        $env:PATH = ((@(
            $managedNode,
            (Join-Path $managedNode "bin"),
            (Join-Path $InstallRoot "venv\Scripts")
        ) | Where-Object { Test-Path $_ }) + @($env:PATH)) -join ";"

        # applyUpdates' preference order: the repo-owned hand-off script when
        # the INSTALLED checkout ships it, else the staged Tauri binary. Run
        # whichever the Update button would actually spawn against this base.
        $handoff = Join-Path $InstallRoot "scripts\desktop-update.ps1"
        $stagedExe = Join-Path $env:HERMES_HOME "hermes-setup.exe"

        if (Test-Path $handoff) {
            # powershell.exe (5.1), not pwsh: that is what the desktop spawns.
            # -NoUi is the script's own headless switch; -DesktopPid 0 skips
            # the wait-for-desktop gate (no desktop is running); no
            # -RelaunchExe so nothing is launched afterwards.
            $handoffLog = Join-Path $LogDir "desktop-update.log"
            & powershell -NoProfile -ExecutionPolicy Bypass -File $handoff `
                -InstallRoot $InstallRoot -Branch main -DesktopPid 0 -NoUi 2>&1 |
                Tee-Object -FilePath $handoffLog
            $handoffExit = $LASTEXITCODE

            $handoffInternalLog = Join-Path $env:HERMES_HOME "logs\desktop-update-handoff.log"
            if (Test-Path $handoffInternalLog) { Copy-Item $handoffInternalLog $LogDir -Force }
            if ($handoffExit -ne 0) {
                Fail "desktop-update.ps1 failed (exit $handoffExit) -- see desktop-update.log + desktop-update-handoff.log"
            }
        } elseif (Test-Path $stagedExe) {
            # Update mode is a hand-off, not a click-through: --update jumps
            # straight to progress and start_update runs unattended, exiting
            # when done -- no AHK needed. On success it auto-launches the
            # desktop; kill that below rather than letting it hold the venv.
            Write-Host "  installed base predates desktop-update.ps1; using staged hermes-setup.exe --update"
            $upd = Start-Process -FilePath $stagedExe -ArgumentList "--update" -PassThru
            if (-not $upd.WaitForExit(45 * 60 * 1000)) {
                Stop-Process -Id $upd.Id -Force -ErrorAction SilentlyContinue
                Fail "hermes-setup.exe --update still running after 45 minutes"
            }
            $updLog = Join-Path $env:HERMES_HOME "logs\update.log"
            if (Test-Path $updLog) { Copy-Item $updLog $LogDir -Force }
            if ($upd.ExitCode -ne 0) {
                Fail "hermes-setup.exe --update failed (exit $($upd.ExitCode)) -- see update.log"
            }
            # The successful updater relaunches Hermes; a live desktop locks
            # the venv shim and would poison later assertions.
            Get-Process -Name "Hermes" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        } else {
            Fail "neither scripts/desktop-update.ps1 (in the installed base) nor a staged hermes-setup.exe exists -- no desktop update path to exercise"
        }

        $After = Invoke-Git @("rev-parse", "HEAD") $InstallRoot
        if ($After -ne $TargetSha) {
            Fail "desktop update left HEAD at $After, wanted $TargetSha"
        }
        Ok "desktop update landed on $($After.Substring(0,12))"

        $version = & $HermesExe --version 2>&1
        if ($LASTEXITCODE -ne 0) { Fail "hermes --version failed after update: $version" }
        Write-Host "    $version"
        Ok "hermes runs after desktop update"
    }
}

Write-Host ""
Write-Host "PASS: Windows install/update E2E (route: $Route, base: $($BaseSha.Substring(0,12)) -> $($TargetSha.Substring(0,12)))" -ForegroundColor Green
exit 0
