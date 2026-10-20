# bundle_local.ps1
# Local (Windows): pack the picoquic submodule's `work` branch into a bundle (full history).
# Usage: powershell -ExecutionPolicy Bypass -File .\sync\bundle_local.ps1
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$sub  = Join-Path $root 'picoquic'
$out  = Join-Path $root 'sync\picoquic_work.bundle'

if (-not (Test-Path (Split-Path $out))) { New-Item -ItemType Directory (Split-Path $out) | Out-Null }

$branch = git -C $sub symbolic-ref --short HEAD
if ($LASTEXITCODE -ne 0 -or $branch -ne 'work') { throw 'picoquic must already be on work' }
$dirty = git -C $sub status --porcelain --untracked-files=no
if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'commit picoquic changes before creating the bundle' }
$expected = git -C $root rev-parse HEAD:picoquic
if ($LASTEXITCODE -ne 0) { throw 'cannot read committed outer gitlink' }
$actual = git -C $sub rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $actual -ne $expected) { throw 'commit the outer picoquic gitlink before bundling' }
# Include annotated release/rollback tags as well as the complete work history.
git -C $sub bundle create $out work --tags
if ($LASTEXITCODE -ne 0) { throw 'bundle create failed' }

Write-Host ''
Write-Host "Bundle created: $out"
git -C $sub bundle verify $out
if ($LASTEXITCODE -ne 0) { throw 'bundle verification failed' }
Get-FileHash -LiteralPath $out -Algorithm SHA256
Write-Host ''
Write-Host 'Submodule pointer (must be clean, no + prefix):'
git -C $root submodule status
Write-Host ''
Write-Host "Next: scp $out user@server:/path/to/  then run sync/bundle_server.sh on the server."
