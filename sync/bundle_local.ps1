# bundle_local.ps1
# Local (Windows): pack the picoquic submodule's `work` branch into a bundle (full history).
# Usage: powershell -ExecutionPolicy Bypass -File .\sync\bundle_local.ps1
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$sub  = Join-Path $root 'picoquic'
$out  = Join-Path $root 'sync\picoquic_work.bundle'

if (-not (Test-Path (Split-Path $out))) { New-Item -ItemType Directory (Split-Path $out) | Out-Null }

Push-Location $sub
git checkout work
if ($LASTEXITCODE -ne 0) { throw 'checkout work failed' }
git bundle create $out work
if ($LASTEXITCODE -ne 0) { throw 'bundle create failed' }
Pop-Location

Write-Host ''
Write-Host "Bundle created: $out"
git -C $root bundle verify $out
Write-Host ''
Write-Host 'Submodule pointer (must be clean, no + prefix):'
git -C $root submodule status
Write-Host ''
Write-Host "Next: scp $out user@server:/path/to/  then run sync/bundle_server.sh on the server."
