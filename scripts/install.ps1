# Hermes Agent bootstrap: git checkout + venv + hermes command on PATH.
# Heavy dependencies (tool binaries, browsers, node) are pm's job after
# this: `hermes pm install`. Stage protocol kept for Hermes-Setup:
#   -Manifest             print the stage list as JSON
#   -Stage NAME [-Json]   run one stage
#   -NonInteractive       skip stages that need input
#   -IncludeDesktop       add the desktop build stage
#   -ProtocolVersion      print the stage protocol version
param(
    [string]$Branch = "main",
    [string]$Commit = "",
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:LOCALAPPDATA\hermes" }),
    [string]$InstallDir = $(if ($env:HERMES_HOME) { "$env:HERMES_HOME\hermes-agent" } else { "$env:LOCALAPPDATA\hermes\hermes-agent" }),
    [switch]$Manifest,
    [string]$Stage,
    [switch]$ProtocolVersion,
    [switch]$NonInteractive,
    [switch]$Json,
    [switch]$IncludeDesktop
)

$ErrorActionPreference = "Stop"
$RepoUrl = if ($env:HERMES_REPO_URL) { $env:HERMES_REPO_URL } else { "https://github.com/NousResearch/hermes-agent.git" }

# --- BEGIN GENERATED: bootstrap pins (scripts/gen-bootstrap-pins.py) ---
# Derived from pm/lock.json. DO NOT EDIT BY HAND:
# run scripts/gen-bootstrap-pins.py after a pin bump.
$script:UvPinVersion = "0.12.3"
$script:UvPinFiles = @{
    "win32-x64" = @{
        Url    = "https://github.com/astral-sh/uv/releases/download/0.12.3/uv-x86_64-pc-windows-msvc.zip"
        Sha256 = "b23350c79e8ad0192b8124af13a0f17e8d4e4549524785e1aef389ae5a06990e"
    }
    "win32-arm64" = @{
        Url    = "https://github.com/astral-sh/uv/releases/download/0.12.3/uv-aarch64-pc-windows-msvc.zip"
        Sha256 = "4343217d668727b8a8eb5cad92389a1d2eeead93c89940d1b955ba1bb15462eb"
    }
}

$script:GitPinVersion = "2.53.0+3"
$script:GitPinFiles = @{
    "win32-x64" = @{
        Url    = "https://github.com/git-for-windows/git/releases/download/v2.53.0.windows.3/Git-2.53.0.3-64-bit.tar.bz2"
        Sha256 = "1661f02e85a7901ad7920e2a358ee3772ed9066b00d8590bf2d9046ef10aa8b2"
    }
    "win32-arm64" = @{
        Url    = "https://github.com/git-for-windows/git/releases/download/v2.53.0.windows.3/Git-2.53.0.3-arm64.tar.bz2"
        Sha256 = "4015f05a68bd2bcf3cc6c426e8d44b65d670fbb879225bb7b7c347cfc3a2758a"
    }
}
# --- END GENERATED: bootstrap pins ---

# Resolve the pm store root (same resolution as pm's store_root()):
# $env:HERMES_RUNTIME_DIR wins, else <HermesHome>\tools.
function Get-PmStoreRoot {
    if ($env:HERMES_RUNTIME_DIR) { return $env:HERMES_RUNTIME_DIR }
    return (Join-Path $HermesHome "tools")
}

# The MACHINE's architecture (registry PROCESSOR_ARCHITECTURE), not the
# interpreter's — an x64 powershell on Windows-on-ARM must stage arm64.
function Get-WindowsArch {
    $machineArch = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment' -ErrorAction SilentlyContinue).PROCESSOR_ARCHITECTURE
    if ($machineArch -eq 'ARM64') { return 'arm64' }
    return 'x64'
}

# Provision uv for this host from the pinned pm/lock.json artifact. Stages
# the EXACT artifact pm itself uses into the same store slot
# (<store>\uv-<version>-<target>\), sha256-verified, so pm adopts the same
# bytes — no astral-latest, no irm|iex. Returns the uv.exe path.
function Get-Uv {
    $existing = Get-Command uv -ErrorAction SilentlyContinue
    if ($existing) { return $existing.Source }  # dev shortcut; fetches nothing
    $target = "win32-$(Get-WindowsArch)"
    $pin = $script:UvPinFiles[$target]
    if (-not $pin) {
        Fail "no pinned uv artifact for $target; install uv manually: https://docs.astral.sh/uv/"
    }
    $entry = Join-Path (Get-PmStoreRoot) "uv-$($script:UvPinVersion)-$target"
    $uvExe = Join-Path $entry "uv.exe"
    if (Test-Path $uvExe) { return $uvExe }
    Log "staging pinned uv $($script:UvPinVersion) ($target) into the pm store"
    $tmpDir = Join-Path ([IO.Path]::GetTempPath()) "hermes-uv-bootstrap-$PID"
    try {
        New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
        $zipPath = Join-Path $tmpDir "uv.zip"
        Invoke-WebRequest -Uri $pin.Url -OutFile $zipPath -UseBasicParsing
        # Digest check BEFORE extraction — a mismatched archive is deleted,
        # never unpacked.
        $digest = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($digest -ne $pin.Sha256.ToLowerInvariant()) {
            Fail "uv digest mismatch (expected $($pin.Sha256), got $digest)"
        }
        $extractDir = Join-Path $tmpDir "unpacked"
        Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
        # The zip carries uv.exe (+ uvx.exe) at the root or under one
        # versioned wrapper dir — take whichever layout arrived.
        $found = Get-ChildItem -Path $extractDir -Filter "uv.exe" -Recurse | Select-Object -First 1
        if (-not $found) { Fail "uv.exe not found in the downloaded archive" }
        New-Item -ItemType Directory -Force -Path $entry | Out-Null
        Move-Item -Path $found.FullName -Destination $uvExe -Force
        $uvx = Get-ChildItem -Path $extractDir -Filter "uvx.exe" -Recurse | Select-Object -First 1
        if ($uvx) { Move-Item -Path $uvx.FullName -Destination (Join-Path $entry "uvx.exe") -Force }
    } finally {
        Remove-Item -Path $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not (& $uvExe --version 2>$null)) { Fail "pinned uv staged but does not run on this host" }
    return $uvExe
}

# Provision git for this host from the pinned pm/lock.json artifact, into
# the same store slot (<store>\git-<version>-<target>\) pm uses. Returns the
# git.exe path, or $null when no pinned artifact exists for this target.
function Get-PinnedGit {
    $existing = Get-Command git -ErrorAction SilentlyContinue
    if ($existing) { return $existing.Source }  # dev shortcut; fetches nothing
    $target = "win32-$(Get-WindowsArch)"
    $pin = $script:GitPinFiles[$target]
    if (-not $pin) { return $null }
    $entry = Join-Path (Get-PmStoreRoot) "git-$($script:GitPinVersion)-$target"
    $gitExe = Join-Path $entry "cmd\git.exe"
    if (Test-Path $gitExe) { return $gitExe }
    Log "staging pinned git $($script:GitPinVersion) ($target) into the pm store"
    $tmpDir = Join-Path ([IO.Path]::GetTempPath()) "hermes-git-bootstrap-$PID"
    try {
        New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
        $tarPath = Join-Path $tmpDir "git.tar.bz2"
        Invoke-WebRequest -Uri $pin.Url -OutFile $tarPath -UseBasicParsing
        # Digest check BEFORE extraction — the archive IS code.
        $digest = (Get-FileHash -Path $tarPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($digest -ne $pin.Sha256.ToLowerInvariant()) {
            Fail "git digest mismatch (expected $($pin.Sha256), got $digest)"
        }
        $extractDir = Join-Path $tmpDir "unpacked"
        New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
        # The pinned artifact is a git-for-windows tar.bz2 (the same one pm
        # itself extracts). Windows 10+ ships bsdtar with bzip2 support.
        & tar.exe -xf $tarPath -C $extractDir
        if ($LASTEXITCODE) { Fail "failed to extract pinned git archive" }
        # Layout: Git-<ver>/cmd\git.exe — flatten the single wrapper dir.
        $inner = @(Get-ChildItem $extractDir)
        $src = $extractDir
        if ($inner.Count -eq 1 -and $inner[0].PSIsContainer) { $src = $inner[0].FullName }
        if (-not (Test-Path (Join-Path $src "cmd\git.exe"))) { Fail "git.exe not found in the downloaded archive" }
        if (Test-Path $entry) { Remove-Item -Recurse -Force $entry }
        Move-Item $src $entry
    } finally {
        Remove-Item -Path $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    return $gitExe
}

# Ensure a usable git for the rest of the ladder: pinned pm store slot
# first, then PATH. Returns $true on success.
function Ensure-Git {
    $g = Get-PinnedGit
    if (-not $g) { return $false }
    if ($g -ne "git") {
        # Store-staged git: expose cmd + usr\bin on this process's PATH so
        # bare `git` works for the rest of the ladder (the same dirs pm's
        # git package env() composes).
        $gitEntry = Split-Path (Split-Path $g -Parent) -Parent
        $env:Path = "$gitEntry\cmd;$gitEntry\usr\bin;$env:Path"
    }
    return $true
}

function Log([string]$msg) { Write-Host "[hermes] $msg" -ForegroundColor Blue }
function Fail([string]$msg) { Write-Host "[hermes] $msg" -ForegroundColor Red; exit 1 }

function Emit-Frame([bool]$ok, [string]$name, [bool]$skipped, [string]$reason = "") {
    $frame = [ordered]@{ ok = $ok; stage = $name; skipped = $skipped }
    if ($reason) { $frame.reason = $reason }
    $frame | ConvertTo-Json -Compress | Write-Output
}

$Stages = @(
    @{ name = "prerequisites"; title = "System prerequisites"; category = "runtime"; needs_user_input = $false },
    @{ name = "repository"; title = "Download Hermes Agent"; category = "runtime"; needs_user_input = $false },
    @{ name = "venv"; title = "Create Python environment"; category = "runtime"; needs_user_input = $false },
    @{ name = "python-deps"; title = "Install Python dependencies"; category = "runtime"; needs_user_input = $false },
    @{ name = "node-deps"; title = "Install tool dependencies"; category = "runtime"; needs_user_input = $false },
    @{ name = "path"; title = "Install hermes command"; category = "runtime"; needs_user_input = $false },
    @{ name = "config"; title = "Prepare config and skills"; category = "configuration"; needs_user_input = $false },
    @{ name = "setup"; title = "Configure API keys and settings"; category = "configuration"; needs_user_input = $true },
    @{ name = "gateway"; title = "Configure gateway service"; category = "configuration"; needs_user_input = $true }
)
if ($IncludeDesktop) {
    $Stages += @{ name = "desktop"; title = "Build desktop app"; category = "runtime"; needs_user_input = $false }
}
$Stages += @{ name = "complete"; title = "Finish install"; category = "runtime"; needs_user_input = $false }

function Stage-Prerequisites {
    if (-not (Ensure-Git)) {
        Fail "git is required. Install Git for Windows: https://git-scm.com/download/win"
    }
    Log "prerequisites ok (git)"
}

function Stage-Repository {
    if (Test-Path (Join-Path $InstallDir ".git")) {
        Log "updating $InstallDir"
        git -C $InstallDir fetch origin $Branch; if ($LASTEXITCODE) { Fail "git fetch failed" }
        git -C $InstallDir checkout $Branch; if ($LASTEXITCODE) { Fail "git checkout failed" }
        git -C $InstallDir pull --ff-only origin $Branch
        if ($LASTEXITCODE) { Log "not fast-forwardable; keeping local state" }
    } else {
        Log "cloning $RepoUrl ($Branch) into $InstallDir"
        New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir) | Out-Null
        git clone --branch $Branch $RepoUrl $InstallDir; if ($LASTEXITCODE) { Fail "git clone failed" }
    }
    if ($Commit) {
        git -C $InstallDir checkout $Commit; if ($LASTEXITCODE) { Fail "could not pin commit $Commit" }
    }
}

function Stage-Venv {
    $uv = Get-Uv
    Log "creating venv"
    Push-Location $InstallDir
    & $uv venv --allow-existing venv; $code = $LASTEXITCODE
    Pop-Location
    if ($code) { Fail "uv venv failed" }
}

function Stage-PythonDeps {
    $uv = Get-Uv
    Log "syncing python dependencies (uv sync --frozen)"
    Push-Location $InstallDir
    $env:VIRTUAL_ENV = Join-Path $InstallDir "venv"
    & $uv sync --frozen --extra all --active; $code = $LASTEXITCODE
    Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
    Pop-Location
    if ($code) { Fail "uv sync failed" }
}

function Stage-NodeDeps {
    Log "tool dependencies are managed by pm (hermes pm install)"
}

function Stage-Path {
    $python = Join-Path $InstallDir "venv\Scripts\python.exe"
    $entry = Join-Path $InstallDir "hermes"
    $binDir = Join-Path $HermesHome "bin"
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    # Copy the console-script launchers from the venv so `hermes` /
    # `hermes-acp` resolve through the managed interpreter without touching
    # the venv\Scripts dir on PATH (which would shadow the user's python).
    foreach ($name in @("hermes.exe", "hermes-acp.exe")) {
        $src = Join-Path $InstallDir "venv\Scripts\$name"
        if (Test-Path $src) {
            Copy-Item -Path $src -Destination (Join-Path $binDir $name) -Force
        }
    }
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$binDir*") {
        [Environment]::SetEnvironmentVariable("Path", "$binDir;$userPath", "User")
        Log "added $binDir to your user PATH (new shells pick it up)"
    }
    Log "hermes command installed at $binDir"
}

function Stage-Config {
    foreach ($d in @("cron","sessions","logs","pairing","hooks","image_cache","audio_cache","memories","skills")) {
        New-Item -ItemType Directory -Force -Path (Join-Path $HermesHome $d) | Out-Null
    }
    $envFile = Join-Path $HermesHome ".env"
    if (-not (Test-Path $envFile)) {
        $example = Join-Path $InstallDir ".env.example"
        if (Test-Path $example) { Copy-Item $example $envFile } else { New-Item -ItemType File -Path $envFile | Out-Null }
    }
    $cfg = Join-Path $HermesHome "config.yaml"
    $cfgExample = Join-Path $InstallDir "cli-config.yaml.example"
    if (-not (Test-Path $cfg) -and (Test-Path $cfgExample)) { Copy-Item $cfgExample $cfg }
    Log "config prepared in $HermesHome"
}

function Stage-Setup {
    if ($NonInteractive) { return }
    & (Join-Path $InstallDir "venv\Scripts\python.exe") (Join-Path $InstallDir "hermes") setup
}

function Stage-Gateway {
    if ($NonInteractive) { return }
    & (Join-Path $InstallDir "venv\Scripts\python.exe") (Join-Path $InstallDir "hermes") gateway install
}

function Stage-Desktop {
    & (Join-Path $InstallDir "venv\Scripts\python.exe") (Join-Path $InstallDir "hermes") desktop build
    if ($LASTEXITCODE) { Fail "desktop build failed" }
}

function Stage-Complete {
    $commit = $Commit
    if (-not $commit) { $commit = git -C $InstallDir rev-parse HEAD 2>$null }
    if ($commit) {
        $marker = [ordered]@{
            schemaVersion = 1
            pinnedCommit = "$commit"
            pinnedBranch = $Branch
            completedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
        }
        $markerPath = Join-Path $InstallDir ".hermes-bootstrap-complete"
        $tmp = "$markerPath.tmp"
        $marker | ConvertTo-Json | Set-Content -Path $tmp -Encoding utf8
        Move-Item -Force $tmp $markerPath
    }
    Log "install complete. Run: hermes"
}

function Invoke-StageByName([string]$name) {
    switch ($name) {
        "prerequisites" { Stage-Prerequisites }
        "repository" { Stage-Repository }
        "venv" { Stage-Venv }
        "python-deps" { Stage-PythonDeps }
        "node-deps" { Stage-NodeDeps }
        "path" { Stage-Path }
        "config" { Stage-Config }
        "setup" { Stage-Setup }
        "gateway" { Stage-Gateway }
        "desktop" { Stage-Desktop }
        "complete" { Stage-Complete }
        default { Write-Error "unknown stage: $name"; exit 2 }
    }
}

if ($ProtocolVersion) { Write-Output 1; exit 0 }

if ($Manifest) {
    @{ protocol_version = 1; stages = $Stages } | ConvertTo-Json -Depth 4 -Compress | Write-Output
    exit 0
}

if ($Stage) {
    $known = @($Stages | ForEach-Object { $_.name })
    if ($known -notcontains $Stage -and $Stage -ne "desktop") {
        if ($Json) { Emit-Frame $false $Stage $false "unknown stage: $Stage" }
        else { [Console]::Error.WriteLine("unknown stage: $Stage") }
        exit 2
    }
    $needsInput = ($Stage -eq "setup") -or ($Stage -eq "gateway")
    if ($NonInteractive -and $needsInput) {
        if ($Json) { Emit-Frame $true $Stage $true "needs user input" }
        exit 0
    }
    try {
        Invoke-StageByName $Stage
        if ($Json) { Emit-Frame $true $Stage $false }
        exit 0
    } catch {
        if ($Json) { Emit-Frame $false $Stage $false "$_" }
        exit 1
    }
}

# No -Stage: run the whole ladder.
foreach ($s in @("prerequisites","repository","venv","python-deps","node-deps","path","config","setup","gateway","complete")) {
    Invoke-StageByName $s
}
