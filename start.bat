@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ====== Beauty Master Bot — Windows one-click launcher ======

cd /d "%~dp0"

echo === Beauty Master Bot ===
echo.

REM 1. Check Python
where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python is not installed or not in PATH.
    echo     Please install Python 3.11+ from https://www.python.org/downloads/
    echo     and tick "Add Python to PATH" during installation.
    pause
    exit /b 1
)

REM 2. Check .env exists
if not exist ".env" (
    echo [!] File .env was not found.
    echo     Copy .env.example to .env and fill in BOT_TOKEN, MASTER_TG_ID, DATABASE_URL.
    pause
    exit /b 1
)

REM 3. Create venv if missing
if not exist ".venv" (
    echo [*] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [!] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

REM 4. Install/update dependencies
echo [*] Installing dependencies...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [!] Failed to install dependencies.
    pause
    exit /b 1
)

REM 5. Run the bot
echo.
echo [*] Starting bot... press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" main.py

pause
