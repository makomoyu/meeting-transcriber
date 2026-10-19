@echo off
setlocal
cd /d "%~dp0"
title GPU transcription server

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Setup has not been completed. Run GPU_PC_初回セットアップ.bat first.
  pause
  exit /b 1
)

if not exist "%~dp0api_key.txt" (
  echo API key file not found. Run GPU_PC_Initial_Setup first.
  pause
  exit /b 1
)

set /p TRANSCRIBE_API_KEY=<"%~dp0api_key.txt"
if not defined TRANSCRIBE_API_KEY (
  echo API key file is empty.
  pause
  exit /b 1
)

set WHISPER_MODEL=medium
echo Starting GPU transcription server. Press Ctrl+C to stop.
echo Keep this window open while the recorder PC is connected.
"%~dp0.venv\Scripts\python.exe" -m uvicorn gpu_server:app --host 0.0.0.0 --port 8765
echo Server stopped.
pause
