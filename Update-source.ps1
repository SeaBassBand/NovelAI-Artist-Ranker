param(
    [string]$Remote = "origin",
    [string]$Branch = "release",
    [string]$PythonExecutable = "",
    [string]$DataRoot = "",
    [switch]$SkipDependencyInstall,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$RepoRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$GitCommand = Get-Command git.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -eq $GitCommand) {
    throw "Git for Windows was not found. Install Git, reopen this folder, and run Update and Start.bat again."
}
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot ".git") -PathType Container)) {
    throw "This folder is not a Git clone. Install with: git clone https://github.com/SeaBassBand/NovelAI-Artist-Ranker.git -b release"
}

function Invoke-Git([string[]]$Arguments) {
    & $GitCommand.Source -C $RepoRoot @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git $($Arguments -join ' ')"
    }
}

$currentBranch = (& $GitCommand.Source -C $RepoRoot rev-parse --abbrev-ref HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $currentBranch -eq "HEAD") {
    throw "The source installation is in detached-HEAD state. Switch back to the release branch before updating."
}
if (-not $currentBranch.Equals($Branch, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "This updater expects branch '$Branch', but the clone is on '$currentBranch'. Switch branches explicitly; no files were changed."
}

$changes = @(& $GitCommand.Source -C $RepoRoot status --porcelain)
if ($LASTEXITCODE -ne 0) { throw "Git could not inspect the working tree." }
if ($changes.Count -gt 0) {
    Write-Host "Local changes were found:" -ForegroundColor Yellow
    $changes | ForEach-Object { Write-Host "  $_" }
    throw "Update stopped without changing anything. Commit, stash, or move the local changes, then try again."
}

Write-Host "[Artist Ranker] Fetching $Remote/$Branch" -ForegroundColor Cyan
Invoke-Git -Arguments @("fetch", "--prune", $Remote, $Branch)
Write-Host "[Artist Ranker] Applying a fast-forward-only update" -ForegroundColor Cyan
Invoke-Git -Arguments @("pull", "--ff-only", $Remote, $Branch)
$commit = (& $GitCommand.Source -C $RepoRoot rev-parse --short HEAD).Trim()
Write-Host "[Artist Ranker] Source is current at $commit" -ForegroundColor Green

$Bootstrap = Join-Path $RepoRoot "Install-from-source.ps1"
$BootstrapArguments = @{}
if ($PythonExecutable) { $BootstrapArguments["PythonExecutable"] = $PythonExecutable }
if ($DataRoot) { $BootstrapArguments["DataRoot"] = $DataRoot }
if ($SkipDependencyInstall) { $BootstrapArguments["SkipDependencyInstall"] = $true }
if ($NoLaunch) { $BootstrapArguments["NoLaunch"] = $true }
& $Bootstrap @BootstrapArguments
exit $LASTEXITCODE
