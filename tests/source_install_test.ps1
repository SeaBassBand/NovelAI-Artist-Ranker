param(
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$TestRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("artist-ranker-source-install-" + [Guid]::NewGuid().ToString("N"))
$InstallRoot = Join-Path $TestRoot "installed"
$DataRoot = Join-Path $TestRoot "user-data"
$RemoteRoot = Join-Path $TestRoot "remote.git"
$PublisherRoot = Join-Path $TestRoot "publisher"

function Require([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Run-ChildPowerShell([string]$Script, [string[]]$Arguments) {
    $command = Get-Command powershell.exe -ErrorAction Stop
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& $command.Source -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Script @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Output = ($output -join "`n") }
}

function Run-Git([string[]]$Arguments) {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& git.exe @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) {
        throw "git $($Arguments -join ' ') failed:`n$($output -join "`n")"
    }
    return $output
}

try {
    New-Item -ItemType Directory -Force -Path (Join-Path $InstallRoot "src") | Out-Null
    foreach ($name in @("Install-from-source.ps1", "Update-source.ps1", "Install.bat", "Start.bat", "Update and Start.bat", ".gitignore", "SOURCE_INSTALL.md")) {
        Copy-Item -LiteralPath (Join-Path $RepositoryRoot $name) -Destination (Join-Path $InstallRoot $name)
    }
    [System.IO.File]::WriteAllText((Join-Path $InstallRoot "requirements.lock.txt"), "", [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText((Join-Path $InstallRoot "README.md"), "fixture`n", [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText((Join-Path $InstallRoot "src\artist_elo_ranker_buffered.py"), "print('fixture')`n", [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText((Join-Path $InstallRoot "src\config.example.py"), "VALUE = 1`n", [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText((Join-Path $InstallRoot "src\danbooru_artist_tags_v4.5.txt"), "fixture_artist`n", [System.Text.UTF8Encoding]::new($false))

    $installArguments = @(
        "-NoLaunch", "-SkipDependencyInstall",
        "-PythonExecutable", $PythonExecutable,
        "-DataRoot", $DataRoot
    )
    $firstInstall = Run-ChildPowerShell (Join-Path $InstallRoot "Install-from-source.ps1") $installArguments
    Require ($firstInstall.ExitCode -eq 0) "First source install failed:`n$($firstInstall.Output)"
    Require ($firstInstall.Output -match "SOURCE_INSTALL_READY") "The installer did not report readiness."
    Require (Test-Path -LiteralPath (Join-Path $InstallRoot ".venv\Scripts\python.exe") -PathType Leaf) "The source venv was not created."
    Require (Test-Path -LiteralPath (Join-Path $InstallRoot "src\config.py") -PathType Leaf) "The local config was not created."
    Require (Test-Path -LiteralPath (Join-Path $InstallRoot ".source-install\source-install.json") -PathType Leaf) "The source-install state was not written."
    Require (Test-Path -LiteralPath $DataRoot -PathType Container) "The external data folder was not created."
    Require (-not $DataRoot.StartsWith($InstallRoot, [System.StringComparison]::OrdinalIgnoreCase)) "Test data was placed inside the repository."

    $secondInstall = Run-ChildPowerShell (Join-Path $InstallRoot "Install-from-source.ps1") $installArguments
    Require ($secondInstall.ExitCode -eq 0) "Repeat source install failed:`n$($secondInstall.Output)"
    Require ($secondInstall.Output -match "reusing the existing environment") "Repeat install did not reuse the dependency environment."

    Run-Git @("init", "--bare", $RemoteRoot) | Out-Null
    Run-Git @("-C", $InstallRoot, "init", "-b", "release") | Out-Null
    Run-Git @("-C", $InstallRoot, "config", "user.email", "source-install-test@example.invalid") | Out-Null
    Run-Git @("-C", $InstallRoot, "config", "user.name", "Source Install Test") | Out-Null
    Run-Git @("-C", $InstallRoot, "add", ".") | Out-Null
    Run-Git @("-C", $InstallRoot, "commit", "-m", "fixture") | Out-Null
    Run-Git @("-C", $InstallRoot, "remote", "add", "origin", $RemoteRoot) | Out-Null
    Run-Git @("-C", $InstallRoot, "push", "-u", "origin", "release") | Out-Null

    Run-Git @("clone", "-b", "release", $RemoteRoot, $PublisherRoot) | Out-Null
    Run-Git @("-C", $PublisherRoot, "config", "user.email", "source-install-test@example.invalid") | Out-Null
    Run-Git @("-C", $PublisherRoot, "config", "user.name", "Source Install Test") | Out-Null
    Add-Content -LiteralPath (Join-Path $PublisherRoot "SOURCE_INSTALL.md") -Value "`nFast-forward test marker."
    Run-Git @("-C", $PublisherRoot, "add", "SOURCE_INSTALL.md") | Out-Null
    Run-Git @("-C", $PublisherRoot, "commit", "-m", "remote update") | Out-Null
    Run-Git @("-C", $PublisherRoot, "push", "origin", "release") | Out-Null

    $updateArguments = @(
        "-NoLaunch", "-SkipDependencyInstall",
        "-PythonExecutable", $PythonExecutable,
        "-DataRoot", $DataRoot
    )
    $update = Run-ChildPowerShell (Join-Path $InstallRoot "Update-source.ps1") $updateArguments
    Require ($update.ExitCode -eq 0) "Fast-forward source update failed:`n$($update.Output)"
    Require ((Get-Content -Raw -LiteralPath (Join-Path $InstallRoot "SOURCE_INSTALL.md")) -match "Fast-forward test marker") "The remote commit was not applied."

    Add-Content -LiteralPath (Join-Path $InstallRoot "README.md") -Value "dirty marker"
    $headBefore = (Run-Git @("-C", $InstallRoot, "rev-parse", "HEAD") | Select-Object -Last 1).Trim()
    $dirtyUpdate = Run-ChildPowerShell (Join-Path $InstallRoot "Update-source.ps1") $updateArguments
    $headAfter = (Run-Git @("-C", $InstallRoot, "rev-parse", "HEAD") | Select-Object -Last 1).Trim()
    Require ($dirtyUpdate.ExitCode -ne 0) "The updater accepted a dirty working tree."
    Require ($dirtyUpdate.Output -match "Update stopped without changing anything") "The dirty-tree safeguard did not explain the refusal."
    Require ($headBefore -eq $headAfter) "The dirty-tree refusal changed the checked-out commit."

    Write-Output "SOURCE_INSTALL_WORKFLOW_OK"
} finally {
    if (Test-Path -LiteralPath $TestRoot) {
        $resolvedTestRoot = [System.IO.Path]::GetFullPath($TestRoot)
        $resolvedTempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        if ($resolvedTestRoot.StartsWith($resolvedTempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
