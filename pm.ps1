# ./pm.ps1 — dev bootstrapper. Uses the pinned uv from pm/lock.json
# (installing it into the pm store if missing), then runs `python -m pm.cli`
# through it. No system python, no system uv: the lockfile is the only
# authority. Usage: ./pm.ps1 <verb> [...]; plain ./pm.ps1 = develop
# (install everything + venv, then drop into an activated subshell).
$ErrorActionPreference = 'Stop'

$repo = $PSScriptRoot
$lock = (Get-Content -Raw (Join-Path $repo 'pm/lock.json')) | ConvertFrom-Json

$machineArch = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment').PROCESSOR_ARCHITECTURE
$arch = if ($machineArch -eq 'ARM64') { 'arm64' } else { 'x64' }
$target = "win32-$arch"

$uvPin = $lock.packages.uv
if (-not $uvPin) { throw 'pm: no uv pin in pm/lock.json' }
$artifact = $uvPin.artifacts.$target
if (-not $artifact) { $artifact = $uvPin.artifacts.any }
if (-not $artifact) { throw "pm: no uv artifact for $target" }

$pyPin = $lock.packages.python
$pyVersion = if ($pyPin) { ($pyPin.version -split '\+')[0] -replace '^(\d+\.\d+).*', '$1' } else { '3.11' }

$store = if ($env:HERMES_RUNTIME_DIR) { $env:HERMES_RUNTIME_DIR } else { Join-Path $HOME '.hermes/tools' }
$entry = Join-Path $store "uv-$($uvPin.version)-$target"
$uv = Join-Path $entry 'uv.exe'

if (-not (Test-Path $uv)) {
    Write-Host "pm: fetching pinned uv $($uvPin.version) ($target)"
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("pm-bootstrap-" + [guid]::NewGuid().ToString('n'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $archive = Join-Path $tmp ([uri]$artifact.url).Segments[-1]
        Invoke-WebRequest -Uri $artifact.url -OutFile $archive
        $got = (Get-FileHash -Algorithm SHA256 $archive).Hash.ToLowerInvariant()
        if ($got -ne $artifact.sha256) {
            throw "pm: sha256 mismatch for uv (got $got, pinned $($artifact.sha256))"
        }
        $tree = Join-Path $tmp 'tree'
        Expand-Archive -Path $archive -DestinationPath $tree
        # flatten a single wrapping dir
        $inner = @(Get-ChildItem $tree)
        $src = if ($inner.Count -eq 1 -and $inner[0].PSIsContainer) { $inner[0].FullName } else { $tree }
        New-Item -ItemType Directory -Force -Path $store | Out-Null
        if (Test-Path $entry) { Remove-Item -Recurse -Force $entry }
        Move-Item $src $entry
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

Set-Location $repo
$verbs = if ($args.Count) { @($args) } else { @('develop') }
& $uv run --no-project --python $pyVersion python -m pm.cli $verbs
exit $LASTEXITCODE
