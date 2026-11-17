# SPDX-License-Identifier: GPL-3.0-only
# Compatible with Windows PowerShell 5.1; no administrator privileges needed.
param(
    [ValidateSet('Install', 'Launch')][string]$Mode = 'Install',
    [switch]$Cpu,
    [switch]$Cuda,
    [switch]$Reinstall
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ProjectDir = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectDir
$env:PYTHONUTF8 = '1'

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with code $LASTEXITCODE" }
}

function Get-EnvironmentPath {
    $config = Join-Path $ProjectDir 'venv-location.windows.txt'
    if (-not (Test-Path -LiteralPath $config)) {
        Copy-Item -LiteralPath (Join-Path $ProjectDir 'venv-location.windows.example.txt') -Destination $config
    }
    $lines = @(Get-Content -LiteralPath $config -Encoding UTF8 | ForEach-Object { $_.Trim() } |
        Where-Object { $_ -and -not $_.StartsWith('#') })
    if ($lines.Count -ne 1) { throw 'Set exactly one full path in venv-location.windows.txt.' }
    $expanded = [Environment]::ExpandEnvironmentVariables($lines[0])
    if ($expanded -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\[^\\]+)') {
        throw 'The environment path must be absolute, for example D:\PythonEnvs\Magic\.venv.'
    }
    $target = [IO.Path]::GetFullPath($expanded).TrimEnd([char[]]'\/')
    $project = [IO.Path]::GetFullPath($ProjectDir).TrimEnd([char[]]'\/')
    if ($target.Equals($project, [StringComparison]::OrdinalIgnoreCase) -or
        $target.StartsWith($project + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The environment must be OUTSIDE the project folder.'
    }
    return $target
}

function Install-App {
    $pins = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime.json') -Raw | ConvertFrom-Json
    $runtime = Join-Path $env:LOCALAPPDATA 'Magic-Instrumental\runtime'
    $uvDir = Join-Path $runtime "uv-$($pins.uv.version)"
    $uv = Join-Path $uvDir 'uv.exe'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $runtime 'python'
    $env:UV_CACHE_DIR = Join-Path $runtime 'cache'
    $env:UV_NO_CONFIG = '1'
    if (-not (Test-Path -LiteralPath $uv)) {
        $temp = Join-Path $runtime ([Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $temp -Force | Out-Null
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $archive = Join-Path $temp 'uv.zip'
            $url = "https://github.com/astral-sh/uv/releases/download/$($pins.uv.version)/$($pins.uv.windows_x64.archive)"
            Write-Host 'Downloading the verified Python installer (uv)...'
            Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $archive -TimeoutSec 600
            if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $pins.uv.windows_x64.sha256) {
                throw 'Download checksum mismatch. Nothing was executed. Please retry installation.'
            }
            Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $temp 'unpacked')
            $binaries = @(Get-ChildItem -LiteralPath (Join-Path $temp 'unpacked') -Filter uv.exe -Recurse)
            if ($binaries.Count -ne 1) { throw 'Unexpected uv archive contents.' }
            New-Item -ItemType Directory -Path $uvDir -Force | Out-Null
            Copy-Item -LiteralPath $binaries[0].FullName -Destination $uv
        } finally {
            Remove-Item -LiteralPath $temp -Recurse -Force
        }
    }
    Write-Host "Installing private Python $($pins.python) (no global Python changes)..."
    Invoke-Checked $uv @('python', 'install', $pins.python, '--no-bin', '--no-registry')
    $managedPython = & $uv python find $pins.python --managed-python --no-project --no-python-downloads
    if ($LASTEXITCODE -ne 0) { throw 'Could not locate the private Python runtime.' }
    Invoke-Checked $managedPython.Trim() (@((Join-Path $ProjectDir 'bootstrap.py'), '--setup-only') + $script:BootstrapArgs)
}

try {
    if ($Cpu -and $Cuda) { throw 'Choose only one of -Cpu or -Cuda.' }
    $architecture = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $architecture = $env:PROCESSOR_ARCHITEW6432 }
    if ($architecture -ne 'AMD64') { throw 'This release supports Windows x64 only.' }
    $script:BootstrapArgs = @()
    if ($Cpu) { $script:BootstrapArgs += '--cpu' }
    if ($Cuda) { $script:BootstrapArgs += '--cuda' }
    if ($Reinstall) { $script:BootstrapArgs += '--reinstall' }
    $target = Get-EnvironmentPath
    $python = Join-Path $target 'Scripts\python.exe'
    if ($Mode -eq 'Install') {
        Install-App
    } else {
        if ($Cpu -or $Cuda -or $Reinstall) { throw 'Use install.bat with -Cpu, -Cuda or -Reinstall.' }
        if (-not (Test-Path -LiteralPath $python)) { Install-App }
        Invoke-Checked $python @((Join-Path $ProjectDir 'bootstrap.py'), '--launch-only')
    }
    exit 0
} catch {
    Write-Host "Magic-Instrumental failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Check the network connection and venv-location.windows.txt, then run install.bat again.'
    exit 1
}
