param([string]$Log = "")
$ErrorActionPreference = "Stop"
if (-not $Log) { $Log = Join-Path $PSScriptRoot "last-console.log" }
if (-not (Test-Path -LiteralPath $Log -PathType Leaf)) {
  Write-Host "No console log exists yet. Start Artist Ranker first."
  exit 1
}
$text = (Get-Content -LiteralPath $Log -Tail 200 -Encoding UTF8) -join "`r`n"
$profilePath = [Environment]::GetFolderPath("UserProfile")
if ($profilePath) { $text = $text.Replace($profilePath, "<USER_HOME>") }
$text = [regex]::Replace($text, '(?i)C:\\Users\\[^\\\s"'']+', 'C:\Users\<USER>')
$text = [regex]::Replace($text, '(?i)(pst|sk)-[A-Za-z0-9_-]{20,}', '$1-<REDACTED>')
$text = [regex]::Replace($text, '(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)\S+', '$1<REDACTED>')
Set-Clipboard -Value $text
Write-Host "Copied the last 200 sanitized console lines to the clipboard."
