# Pack committed main history/tags; optionally produce a matched picoquic bundle.
# Usage: powershell -ExecutionPolicy Bypass -File .\sync\bundle_main_local.ps1 -IncludePicoquic
param([switch]$IncludePicoquic)
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$out = Join-Path $root 'sync\paper_master_main.bundle'
$manifest = Join-Path $root 'sync\bundles.sha256'

$branch = git -C $root symbolic-ref --short HEAD
if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') { throw 'Paper_master must already be on main' }
$mainHead = git -C $root rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw 'cannot read committed main revision' }
$coreHead = git -C $root rev-parse HEAD:picoquic
if ($LASTEXITCODE -ne 0) { throw 'cannot read committed picoquic gitlink' }
$dirty = git -C $root status --porcelain --untracked-files=normal --ignore-submodules=all
if ($LASTEXITCODE -ne 0) { throw 'cannot inspect main worktree' }
if ($dirty) { Write-Warning 'Uncommitted/untracked files are excluded. Only committed main history and tags will be exported.' }

if ($IncludePicoquic) {
    & (Join-Path $root 'sync\bundle_local.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'picoquic bundle creation failed' }
}

git -C $root bundle create $out main --tags
if ($LASTEXITCODE -ne 0) { throw 'main bundle creation failed' }
git -C $root bundle verify $out
if ($LASTEXITCODE -ne 0) { throw 'main bundle verification failed' }

$mainHeadAfter = git -C $root rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $mainHeadAfter -ne $mainHead) { throw 'main changed during export; regenerate bundles' }
$files = @($out)
if ($IncludePicoquic) { $files += (Join-Path $root 'sync\picoquic_work.bundle') }
$lines = @()
foreach ($file in $files) {
    $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
    $lines += "$hash  $(Split-Path -Leaf $file)"
}
[System.IO.File]::WriteAllText($manifest, ($lines -join "`n") + "`n", [System.Text.Encoding]::ASCII)
Write-Host "Main: $mainHead"
Write-Host "Required picoquic: $coreHead"
foreach ($line in $lines) { Write-Host $line }
Write-Host "Checksum manifest: $manifest"
Write-Host 'Transfer the listed bundles and bundles.sha256 to the server. No GitHub access is required.'
