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
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
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

function Get-Uv {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { return $uv.Source }
    Log "installing uv (astral.sh)"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) { Fail "uv install failed" }
    return $uv.Source
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
    $cmdPath = Join-Path $binDir "hermes.cmd"
    Set-Content -Path $cmdPath -Encoding ascii -Value @(
        "@echo off",
        "set PYTHONPATH=",
        "set PYTHONHOME=",
        "`"$python`" `"$entry`" %*"
    )
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$binDir*") {
        [Environment]::SetEnvironmentVariable("Path", "$binDir;$userPath", "User")
        Log "added $binDir to your user PATH (new shells pick it up)"
    }
    Log "hermes command installed at $cmdPath"
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
