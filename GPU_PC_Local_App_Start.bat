@echo off
setlocal
cd /d "%~dp0"
title Meeting transcription app (local GPU)

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Setup has not been completed. Run GPU_PC_初回セットアップ.bat first.
  pause
  exit /b 1
)

"%~dp0.venv\Scripts\python.exe" "%~dp0app.py"
if errorlevel 1 (
  echo The app stopped with an error.
  pause
)
