@echo off
setlocal
title Apex Trading System Launcher
cd /d "%~dp0"

echo ===================================================
echo           APEX TRADING SYSTEM LAUNCHER
echo ===================================================
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_apex.ps1" %*
if errorlevel 1 (
  echo.
  echo Apex failed to start. Check the error above and the logs folder.
  echo.
  pause
  exit /b 1
)

echo.
echo Both services passed their health checks.
echo Dashboard: http://localhost:5173
echo API:       http://localhost:8080/api/healthz
echo Logs:      %~dp0logs
echo.
pause
