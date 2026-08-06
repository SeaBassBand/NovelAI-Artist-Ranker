@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title NovelAI Artist Ranker - Source update
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Update-source.ps1"
set "RANKER_EXIT=%ERRORLEVEL%"
if not "%RANKER_EXIT%"=="0" (
  echo.
  echo Update or startup failed with exit code %RANKER_EXIT%.
  pause
)
exit /b %RANKER_EXIT%
