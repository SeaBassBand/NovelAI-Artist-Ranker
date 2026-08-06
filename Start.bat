@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title NovelAI Artist Ranker - Source launcher
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-from-source.ps1"
set "RANKER_EXIT=%ERRORLEVEL%"
if not "%RANKER_EXIT%"=="0" (
  echo.
  echo Artist Ranker stopped with exit code %RANKER_EXIT%.
  pause
)
exit /b %RANKER_EXIT%
