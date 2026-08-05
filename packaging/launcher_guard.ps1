param([int]$Port = 7860)
$ErrorActionPreference = "SilentlyContinue"
try {
  $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/public/health" -TimeoutSec 1
  if ($response.ok) {
    Write-Host "Artist Ranker is already running. Opening it now..."
    exit 1
  }
} catch {}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen | Select-Object -First 1
if ($listener) {
  $process = Get-Process -Id $listener.OwningProcess
  $name = if ($process) { $process.ProcessName } else { "unknown process" }
  Write-Host "Port $Port is already used by $name (PID $($listener.OwningProcess))." -ForegroundColor Red
  Write-Host "Close that program, or change Artist Ranker's SERVER_PORT setting, then try again."
  exit 2
}
exit 0
