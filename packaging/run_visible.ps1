param(
  [Parameter(Mandatory=$true)][string]$Python,
  [Parameter(Mandatory=$true)][string]$Script,
  [Parameter(Mandatory=$true)][string]$Log
)
$ErrorActionPreference = "Stop"
$runtimeDir = Split-Path -Parent ([System.IO.Path]::GetFullPath($Python))
$userPaths = Join-Path $runtimeDir "user_paths.json"
if (Test-Path -LiteralPath $userPaths -PathType Leaf) {
  try {
    $paths = Get-Content -LiteralPath $userPaths -Raw | ConvertFrom-Json
    if ($paths.data_root) {
      $dataRoot = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables([string]$paths.data_root))
      $env:ARTIST_RANKER_DATA_DIR = $dataRoot
    }
    if ($paths.local_app_data) {
      $localAppData = [System.IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables([string]$paths.local_app_data))
      $env:LOCALAPPDATA = $localAppData
    }
  } catch {
    throw "runtime\user_paths.json is invalid: $($_.Exception.Message)"
  }
}
$logPath = [System.IO.Path]::GetFullPath($Log)
$logParent = Split-Path -Parent $logPath
New-Item -ItemType Directory -Path $logParent -Force | Out-Null
if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 2097152) {
  $previous = "$logPath.previous"
  if (Test-Path -LiteralPath $previous) { Remove-Item -LiteralPath $previous -Force }
  Move-Item -LiteralPath $logPath -Destination $previous
}
Write-Host "Closing this window or pressing Ctrl+C stops Artist Ranker."
Write-Host "Recent output is retained locally for the sanitized-log command."
Write-Host ""
# Windows PowerShell wraps a native program's stderr lines as ErrorRecord
# objects. Uvicorn writes ordinary INFO startup messages there, so normalize
# the merged stream to text and do not let those lines terminate the launcher.
$previousErrorPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Python $Script 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $logPath -Append
$rankerExit = $LASTEXITCODE
$ErrorActionPreference = $previousErrorPreference
exit $rankerExit
