@echo off
setlocal
cd /d "%~dp0"
title GPU server setup

set "PYTHON_CMD=py"
where py >nul 2>&1
if errorlevel 1 (
  set "PYTHON_CMD=python"
  where python >nul 2>&1
  if errorlevel 1 (
    echo ERROR: Python was not found. Install Python 3.10 or later and try again.
    pause
    exit /b 1
  )
)

echo [1/3] Installing server packages...
"%PYTHON_CMD%" -m pip install --upgrade pip
if errorlevel 1 goto :failed
"%PYTHON_CMD%" -m pip install -r "%~dp0server-requirements.txt"
if errorlevel 1 goto :failed

if not exist "%~dp0api_key.txt" (
  echo [2/3] Creating API key...
  "%PYTHON_CMD%" -c "import secrets; print(secrets.token_urlsafe(32))" > "%~dp0api_key.txt"
  if errorlevel 1 goto :failed
) else (
  echo [2/3] Keeping existing API key.
)

echo [3/3] Setup complete.
echo API key file: "%~dp0api_key.txt"
echo Copy the key into the recorder PC app.
echo GPU mode also needs the NVIDIA driver, CUDA 12 cuBLAS, and cuDNN 9.
pause
exit /b 0

:failed
echo Setup failed. Check the error shown above.
pause
exit /b 1
