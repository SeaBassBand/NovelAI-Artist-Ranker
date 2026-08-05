param([Parameter(Mandatory=$true)][string]$Payload)
$ErrorActionPreference = "Stop"
$install = Join-Path $env:LOCALAPPDATA "Programs\NovelAI Artist Ranker"
$staging = "$install.installing"
$backup = "$install.previous"
$programs = Join-Path ([Environment]::GetFolderPath("Programs")) "NovelAI Artist Ranker"
$desktopLink = Join-Path ([Environment]::GetFolderPath("Desktop")) "NovelAI Artist Ranker.lnk"

function Remove-PathSafe([string]$Path) {
  if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
}

try {
  Remove-PathSafe $staging
  New-Item -ItemType Directory -Path $staging -Force | Out-Null
  Expand-Archive -LiteralPath $Payload -DestinationPath $staging -Force

  $required = @(
    (Join-Path $staging "runtime\pythonw.exe"),
    (Join-Path $staging "runtime\public_launcher.pyw"),
    (Join-Path $staging "runtime\uninstall.pyw"),
    (Join-Path $staging "app\artist_elo_ranker_buffered.py")
  )
  foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Installer payload is incomplete: $path" }
  }

  Remove-PathSafe $backup
  if (Test-Path -LiteralPath $install) { Move-Item -LiteralPath $install -Destination $backup }
  try {
    Move-Item -LiteralPath $staging -Destination $install

    $launcher = Join-Path $install "Launch Artist Ranker.cmd"
    $pythonw = Join-Path $install "runtime\pythonw.exe"
    $uninstaller = Join-Path $install "runtime\uninstall.pyw"
    $ws = New-Object -ComObject WScript.Shell
    New-Item -ItemType Directory -Path $programs -Force | Out-Null
    foreach ($pair in @(
      @($desktopLink, $launcher, "NovelAI Artist Ranker"),
      @((Join-Path $programs "NovelAI Artist Ranker.lnk"), $launcher, "NovelAI Artist Ranker"),
      @((Join-Path $programs "Uninstall NovelAI Artist Ranker.lnk"), $uninstaller, "Uninstall NovelAI Artist Ranker")
    )) {
      $shortcut = $ws.CreateShortcut($pair[0])
      if ($pair[2] -eq "Uninstall NovelAI Artist Ranker") {
        $shortcut.TargetPath = $pythonw
        $shortcut.Arguments = '"' + $pair[1] + '"'
      }
      else {
        $shortcut.TargetPath = $pair[1]
        $shortcut.Arguments = ""
      }
      $shortcut.WorkingDirectory = $install
      $shortcut.Description = $pair[2]
      $shortcut.Save()
    }
  }
  catch {
    Remove-PathSafe $install
    if (Test-Path -LiteralPath $backup) { Move-Item -LiteralPath $backup -Destination $install }
    throw
  }
  Remove-PathSafe $backup
  Start-Process -FilePath (Join-Path $install "runtime\pythonw.exe") -ArgumentList ('"' + (Join-Path $install "runtime\public_launcher.pyw") + '"')
}
catch {
  Remove-PathSafe $staging
  throw
}
