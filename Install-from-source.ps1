param(
    [string]$PythonExecutable = "",
    [string]$DataRoot = "",
    [switch]$ForceDependencies,
    [switch]$SkipDependencyInstall,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$RepoRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$SourceRoot = Join-Path $RepoRoot "src"
$LockFile = Join-Path $RepoRoot "requirements.lock.txt"
$MainScript = Join-Path $SourceRoot "artist_elo_ranker_buffered.py"
$ConfigExample = Join-Path $SourceRoot "config.example.py"
$ConfigFile = Join-Path $SourceRoot "config.py"
$ArtistList = Join-Path $SourceRoot "danbooru_artist_tags_v4.5.txt"
$VenvRoot = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"
$LocalState = Join-Path $RepoRoot ".source-install"
$LockMarker = Join-Path $LocalState "requirements.sha256"
$InstallState = Join-Path $LocalState "source-install.json"

function Write-Step([string]$Message) {
    Write-Host "[Artist Ranker] $Message" -ForegroundColor Cyan
}

function Resolve-CommandPath([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    if (Test-Path -LiteralPath $Value -PathType Leaf) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    $command = Get-Command $Value -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $command) { return [string]$command.Source }
    return $null
}

function Test-Python311([string]$Executable, [string[]]$PrefixArguments) {
    try {
        $version = & $Executable @PrefixArguments -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        return $LASTEXITCODE -eq 0 -and ([string]$version).Trim() -eq "3.11"
    } catch {
        return $false
    }
}

function Find-Python311 {
    if ($PythonExecutable) {
        $explicit = Resolve-CommandPath $PythonExecutable
        if (-not $explicit -or -not (Test-Python311 $explicit @())) {
            throw "The selected Python executable is unavailable or is not Python 3.11: $PythonExecutable"
        }
        return [pscustomobject]@{ Executable = $explicit; Prefix = @() }
    }

    $py = Resolve-CommandPath "py.exe"
    if ($py -and (Test-Python311 $py @("-3.11"))) {
        return [pscustomobject]@{ Executable = $py; Prefix = @("-3.11") }
    }
    foreach ($name in @("python.exe", "python3.11.exe", "python")) {
        $candidate = Resolve-CommandPath $name
        if ($candidate -and (Test-Python311 $candidate @())) {
            return [pscustomobject]@{ Executable = $candidate; Prefix = @() }
        }
    }
    throw "Python 3.11 was not found. Install Python 3.11 for Windows, enable the Python launcher, then run Install.bat again."
}

function Normalize-AbsolutePath([string]$Value) {
    $expanded = [Environment]::ExpandEnvironmentVariables($Value)
    return [System.IO.Path]::GetFullPath($expanded)
}

function Assert-DataOutsideRepository([string]$Candidate) {
    $repo = $RepoRoot.TrimEnd("\")
    $data = $Candidate.TrimEnd("\")
    if ($data.Equals($repo, [System.StringComparison]::OrdinalIgnoreCase) -or
        $data.StartsWith($repo + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The source-install data folder must be outside the Git repository: $Candidate"
    }
}

foreach ($required in @($LockFile, $MainScript, $ConfigExample, $ArtistList)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required source-install file is missing: $required"
    }
}

Write-Step "Checking Python 3.11"
$Python = Find-Python311
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Write-Step "Creating the private .venv environment"
    $PythonLauncher = [string]$Python.Executable
    $PythonPrefix = @($Python.Prefix)
    & $PythonLauncher @PythonPrefix -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        throw "Python could not create $VenvRoot"
    }
}
if (-not (Test-Python311 $VenvPython @())) {
    throw "The existing .venv does not use Python 3.11. Rename or remove .venv, then run Install.bat again."
}

New-Item -ItemType Directory -Force -Path $LocalState | Out-Null
if (-not (Test-Path -LiteralPath $ConfigFile -PathType Leaf)) {
    Copy-Item -LiteralPath $ConfigExample -Destination $ConfigFile
    Write-Step "Created local src\config.py from the public defaults"
}

$savedState = $null
if (Test-Path -LiteralPath $InstallState -PathType Leaf) {
    try { $savedState = Get-Content -Raw -LiteralPath $InstallState | ConvertFrom-Json } catch { $savedState = $null }
}
if ($DataRoot) {
    $SelectedDataRoot = Normalize-AbsolutePath $DataRoot
} elseif ($null -ne $savedState -and $savedState.data_root) {
    $SelectedDataRoot = Normalize-AbsolutePath ([string]$savedState.data_root)
} else {
    $SelectedDataRoot = Normalize-AbsolutePath (Join-Path (Split-Path -Parent $RepoRoot) "NovelAI-Artist-Ranker-Data")
}
Assert-DataOutsideRepository $SelectedDataRoot
try {
    New-Item -ItemType Directory -Force -Path $SelectedDataRoot | Out-Null
} catch {
    if ($DataRoot) { throw }
    $SelectedDataRoot = Normalize-AbsolutePath (Join-Path $env:LOCALAPPDATA "NovelAI Artist Ranker\data")
    Assert-DataOutsideRepository $SelectedDataRoot
    New-Item -ItemType Directory -Force -Path $SelectedDataRoot | Out-Null
}

$LockHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $LockFile).Hash.ToLowerInvariant()
$InstalledHash = ""
if (Test-Path -LiteralPath $LockMarker -PathType Leaf) {
    $InstalledHash = ([string](Get-Content -Raw -LiteralPath $LockMarker)).Trim().ToLowerInvariant()
}
$DependenciesChanged = $ForceDependencies -or $InstalledHash -ne $LockHash
if ($DependenciesChanged) {
    if ($SkipDependencyInstall) {
        Write-Step "Skipping dependency installation for a test fixture"
    } else {
        Write-Step "Installing the locked Python dependencies (first launch can take several minutes)"
        & $VenvPython -m pip install --disable-pip-version-check --requirement $LockFile
        if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
        & $VenvPython -c "import fastapi, gradio, numpy, PIL, pydantic, uvicorn, novelai_python; print('SOURCE_DEPENDENCIES_OK')"
        if ($LASTEXITCODE -ne 0) { throw "The installed environment failed its import check." }
    }
    Set-Content -NoNewline -Encoding ascii -LiteralPath $LockMarker -Value $LockHash
    Write-Step "Dependency environment is current"
} else {
    Write-Step "Dependency lock is unchanged; reusing the existing environment"
}

$state = [ordered]@{
    schema_version = 1
    repository_root = $RepoRoot
    data_root = $SelectedDataRoot
    python = $VenvPython
    requirements_sha256 = $LockHash
    updated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}
$state | ConvertTo-Json | Set-Content -Encoding utf8 -LiteralPath $InstallState

$env:ARTIST_RANKER_DATA_DIR = $SelectedDataRoot
$env:ARTIST_RANKER_SOURCE_INSTALL = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"

Write-Step "Source installation ready"
Write-Host "Repository: $RepoRoot"
Write-Host "Data:       $SelectedDataRoot"
if ($NoLaunch) {
    Write-Output "SOURCE_INSTALL_READY"
    exit 0
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:7860/api/public/health" -TimeoutSec 2
    if ($health.ok) {
        Write-Step "Artist Ranker is already running; opening the browser"
        Start-Process "http://127.0.0.1:7860/ranker/"
        exit 0
    }
} catch {}

Write-Step "Starting Artist Ranker; closing this console stops the server"
Push-Location $SourceRoot
try {
    & $VenvPython $MainScript
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
