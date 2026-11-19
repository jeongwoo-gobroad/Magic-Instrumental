# SPDX-License-Identifier: GPL-3.0-only
# ASCII source keeps PowerShell 5.1 independent of the host code page.
param([string]$ProjectDir)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$UpdaterDir = Split-Path -Parent $PSScriptRoot
try {
    if (-not $ProjectDir) { $ProjectDir = $UpdaterDir }
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir 'main.py'))) {
        Add-Type -AssemblyName System.Windows.Forms
        $picker = New-Object System.Windows.Forms.FolderBrowserDialog
        $picker.Description = 'Select your existing Magic-Instrumental folder (containing targets and start.bat).'
        $picker.ShowNewFolderButton = $false
        if ($picker.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 1 }
        $ProjectDir = $picker.SelectedPath
        $picker.Dispose()
    }
    $config = Join-Path $ProjectDir 'venv-location.windows.txt'
    $lines = @(Get-Content -LiteralPath $config -Encoding UTF8 | ForEach-Object { $_.Trim() } |
        Where-Object { $_ -and -not $_.StartsWith('#') })
    if ($lines.Count -ne 1) { throw 'Expected one external environment path in venv-location.windows.txt.' }
    $python = Join-Path ([Environment]::ExpandEnvironmentVariables($lines[0])) 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Run install.bat in the existing folder first.' }
    $env:PYTHONUTF8 = '1'
    & $python (Join-Path $UpdaterDir 'update.py') --project $ProjectDir --pause --wait-for-app
    exit $LASTEXITCODE
} catch {
    Write-Host "Update failed: $($_.Exception.Message)" -ForegroundColor Red
    if ($env:MAGIC_NO_PAUSE -ne '1') { Read-Host 'Press Enter to close' | Out-Null }
    exit 1
}
