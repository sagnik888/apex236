@echo off
title Apex Trading System Launcher
echo ===================================================
echo           APEX TRADING SYSTEM LAUNCHER
echo ===================================================
echo.

:: Get the directory where the batch file is located
set "ROOT_DIR=%~dp0"

echo [1/3] Terminating any existing instances...
:: Find and kill process listening on port 8080 (backend)
for /f "tokens=5" %%a in ('netstat -aon ^| find ":8080" ^| find "LISTENING"') do taskkill /f /pid %%a >nul 2>&1
:: Find and kill process listening on port 5173 (frontend)
for /f "tokens=5" %%a in ('netstat -aon ^| find ":5173" ^| find "LISTENING"') do taskkill /f /pid %%a >nul 2>&1
timeout /t 1 /nobreak >nul

echo [2/3] Starting Python Backend Server...
:: Launch backend in a new window
start "Apex Backend Server" cmd /k "cd /d %ROOT_DIR% && .venv\Scripts\python.exe artifacts\api-server\python_scanner\main.py"

:: Give the backend a second to initialize
timeout /t 2 /nobreak >nul

echo [3/3] Starting React Dashboard (Vite)...
:: Launch frontend in a new window and log to frontend.log
start "Apex Dashboard" cmd /k "cd /d %ROOT_DIR% && set PORT=5173&& set BASE_PATH=/&& pnpm --filter @workspace/trading-dashboard run dev > frontend.log 2>&1"

:: Give vite a moment to start before opening browser
timeout /t 3 /nobreak >nul
start http://localhost:5173
echo.
echo ===================================================
echo System is booting up!
echo The dashboard should automatically open or be available at:
echo http://localhost:5173
echo.
echo NOTE: 
echo - To stop the system, simply close the two new command windows that opened.
echo ===================================================
echo.
pause
