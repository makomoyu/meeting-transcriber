@echo off
setlocal
cd /d "%~dp0"
title GPU PC setup

set "PYTHON_LAUNCHER=py"
set "PYTHON_ARGS=-3"
where py >nul 2>&1
if errorlevel 1 (
  set "PYTHON_LAUNCHER=python"
  set "PYTHON_ARGS="
  where python >nul 2>&1
  if errorlevel 1 (
    echo ERROR: Python 3.10 or later was not found.
    echo Install Python and enable the option to add it to PATH, then retry.
    pause
    exit /b 1
  )
)

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo [1/5] Creating an isolated Python environment...
  %PYTHON_LAUNCHER% %PYTHON_ARGS% -m venv "%~dp0.venv"
  if errorlevel 1 goto :failed
)

set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"
echo [2/5] Updating pip...
"%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :failed

echo [3/5] Installing the app and API server packages...
"%VENV_PYTHON%" -m pip install -r "%~dp0requirements.txt" -r "%~dp0server-requirements.txt"
if errorlevel 1 goto :failed

echo [4/5] Installing NVIDIA CUDA 12 libraries for faster-whisper...
"%VENV_PYTHON%" -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
if errorlevel 1 goto :failed

if not exist "%~dp0api_key.txt" (
  echo [5/5] Creating an API key for other PCs...
  "%VENV_PYTHON%" -c "import secrets; print(secrets.token_urlsafe(32))" > "%~dp0api_key.txt"
  if errorlevel 1 goto :failed
) else (
  echo [5/5] Keeping the existing API key.
)

echo.
echo Setup complete.
echo Start the local recording app with GPU_PC_Local_App_Start.bat
echo Start the LAN API server with GPU_PC_サーバー起動.bat
echo The NVIDIA display driver must be installed. First model use downloads the selected Whisper model.
pause
exit /b 0

:failed
echo.
echo Setup failed. Review the error above. The virtual environment is at "%~dp0.venv".
pause
exit /b 1
