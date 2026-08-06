@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title NovelAI Artist Ranker - Source installation
echo Installing or validating NovelAI Artist Ranker from source...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-from-source.ps1"
set "RANKER_EXIT=%ERRORLEVEL%"
if not "%RANKER_EXIT%"=="0" (
  echo.
  echo Installation or startup failed with exit code %RANKER_EXIT%.
  pause
)
exit /b %RANKER_EXIT%
