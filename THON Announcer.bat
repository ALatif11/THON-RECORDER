@echo off
setlocal
cd /d "%~dp0"

rem Prefer a local virtualenv if one exists, otherwise fall back to system python.
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

title THON Announcer

"%PY%" -c "import uvicorn, fastapi, markdown" >nul 2>&1
if errorlevel 1 (
    echo First run: installing dependencies. This will take a few minutes.
    echo.
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
    echo.
    echo Installing the optional app window. Safe to fail, it just means
    echo the app opens in your browser instead.
    "%PY%" -m pip install -r requirements-desktop.txt
    echo.
)

echo Starting THON Announcer...
echo (Leave this window open. Closing it quits the app.)
echo.

"%PY%" run.py ui
if errorlevel 1 goto failed
goto :eof

:failed
echo.
echo Something went wrong. The error is above.
pause
