param(
  [Parameter(Mandatory=$true)][string]$Payload,
  [string]$InstallDir = ""
)
$ErrorActionPreference = "Stop"
$registryPath = "HKCU:\Software\SeaBassBand\NovelAI Artist Ranker"
$defaultInstall = Join-Path $env:LOCALAPPDATA "Programs\NovelAI Artist Ranker"

function Select-InstallFolder([string]$Suggested) {
  Add-Type -AssemblyName System.Windows.Forms
  $choice = [System.Windows.Forms.MessageBox]::Show(
    "Choose where Artist Ranker should be installed.`n`nYes: use the suggested location`n$Suggested`n`nNo: choose another folder (including D:).`nCancel: stop setup.",
    "NovelAI Artist Ranker setup",
    [System.Windows.Forms.MessageBoxButtons]::YesNoCancel,
    [System.Windows.Forms.MessageBoxIcon]::Question
  )
  if ($choice -eq [System.Windows.Forms.DialogResult]::Cancel) { throw "Setup was cancelled." }
  if ($choice -eq [System.Windows.Forms.DialogResult]::Yes) { return $Suggested }
  $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
  $dialog.Description = "Choose the parent folder for NovelAI Artist Ranker. The installer will create a NovelAI-Artist-Ranker folder inside it."
  $dialog.ShowNewFolderButton = $true
  if (Test-Path -LiteralPath $Suggested) { $dialog.SelectedPath = $Suggested }
  if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { throw "Setup was cancelled." }
  $selected = [System.IO.Path]::GetFullPath($dialog.SelectedPath)
  if ((Split-Path -Leaf $selected) -match '^NovelAI[- ]Artist[- ]Ranker$') { return $selected }
  return (Join-Path $selected "NovelAI-Artist-Ranker")
}

if (-not $InstallDir) {
  $remembered = ""
  try { $remembered = (Get-ItemProperty -LiteralPath $registryPath -Name InstallDir -ErrorAction Stop).InstallDir } catch {}
  $InstallDir = Select-InstallFolder ($(if ($remembered) { $remembered } else { $defaultInstall }))
}
$install = [System.IO.Path]::GetFullPath($InstallDir)
if ([System.IO.Path]::GetPathRoot($install) -eq $install) { throw "Choose a folder below the drive root." }
$staging = "$install.installing"
$backup = "$install.previous"
$programs = Join-Path ([Environment]::GetFolderPath("Programs")) "NovelAI Artist Ranker"
$desktopLink = Join-Path ([Environment]::GetFolderPath("Desktop")) "NovelAI Artist Ranker.lnk"

function Remove-PathSafe([string]$Path) {
  if (-not $Path) { throw "Refusing to remove an empty path." }
  $resolved = [System.IO.Path]::GetFullPath($Path)
  if ([System.IO.Path]::GetPathRoot($resolved) -eq $resolved) { throw "Refusing to remove a drive root." }
  if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
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

  # Machine-local path choices are deliberately absent from public archives.
  # Preserve them across a full installer replacement when this PC has them.
  $existingUserPaths = Join-Path $install "runtime\user_paths.json"
  $stagedUserPaths = Join-Path $staging "runtime\user_paths.json"
  if (Test-Path -LiteralPath $existingUserPaths -PathType Leaf) {
    Copy-Item -LiteralPath $existingUserPaths -Destination $stagedUserPaths -Force
  }

  Remove-PathSafe $backup
  if (Test-Path -LiteralPath $install) { Move-Item -LiteralPath $install -Destination $backup }
  try {
    Move-Item -LiteralPath $staging -Destination $install
    New-Item -Path $registryPath -Force | Out-Null
    Set-ItemProperty -LiteralPath $registryPath -Name InstallDir -Value $install

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
      } else {
        $shortcut.TargetPath = $pair[1]
        $shortcut.Arguments = ""
      }
      $shortcut.WorkingDirectory = $install
      $shortcut.Description = $pair[2]
      $shortcut.Save()
    }
  } catch {
    Remove-PathSafe $install
    if (Test-Path -LiteralPath $backup) { Move-Item -LiteralPath $backup -Destination $install }
    throw
  }
  Remove-PathSafe $backup
  Start-Process -FilePath (Join-Path $install "Launch Artist Ranker.cmd") -WorkingDirectory $install
} catch {
  Remove-PathSafe $staging
  throw
}
