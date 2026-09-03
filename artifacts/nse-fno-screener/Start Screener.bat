@echo off
setlocal
cd /d "%~dp0"
title NSE F&O Screener
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Creating Python environment...
  %PY% -m venv .venv || goto :fail
)
call ".venv\Scripts\activate.bat" || goto :fail
echo [2/4] Installing/updating dependencies...
python -m pip install -q --disable-pip-version-check -r requirements.txt || goto :fail
if not exist ".env" copy /Y ".env.example" ".env" >nul
echo [3/4] Starting screener at http://127.0.0.1:8000
start "" http://127.0.0.1:8000
echo [4/4] Keep this window open while using the screener. Press Ctrl+C to stop.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
exit /b 0
:fail
echo.
echo Startup failed. Check the error above.
pause
exit /b 1
