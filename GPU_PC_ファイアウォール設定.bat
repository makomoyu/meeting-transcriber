@echo off
setlocal
title GPU server firewall setup

"%SystemRoot%\System32\net.exe" session >nul 2>&1
if errorlevel 1 (
  echo ERROR: Right-click this file and choose "Run as administrator".
  pause
  exit /b 1
)

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -Command "$rule = Get-NetFirewallRule -DisplayName 'Meeting Transcription GPU Server' -ErrorAction SilentlyContinue; if (-not $rule) { New-NetFirewallRule -DisplayName 'Meeting Transcription GPU Server' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -Profile Private -RemoteAddress LocalSubnet | Out-Null; Write-Host 'Allowed TCP 8765 from the local subnet on Private networks.' } else { Write-Host 'Firewall rule already exists.' }"
if errorlevel 1 (
  echo ERROR: Firewall setup failed.
  pause
  exit /b 1
)
pause
