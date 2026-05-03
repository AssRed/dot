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
    echo     Install Python 3.11+ from https://www.python.org/downloads/
    echo     and tick "Add Python to PATH" during installation.
    pause
    exit /b 1
)

REM 2. Auto-create .env from template if missing
if not exist ".env" (
    if exist ".env.example" (
        copy /Y ".env.example" ".env" >nul
        echo [!] Created .env from template.
        echo     Open .env in Notepad, fill in BOT_TOKEN and MASTER_TG_ID, save.
        echo.
        echo     BOT_TOKEN  - get from @BotFather in Telegram
        echo     MASTER_TG_ID - your Telegram numeric id (ask @userinfobot)
        echo.
        notepad .env
        echo.
        echo Press any key when done editing .env...
        pause >nul
    ) else (
        echo [!] No .env or .env.example found.
        pause
        exit /b 1
    )
)

REM 3. Create venv if missing
if not exist ".venv" (
    echo [*] Creating virtual environment (one-time, ~30 sec)...
    python -m venv .venv
    if errorlevel 1 (
        echo [!] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

REM 4. Install/update dependencies (silent if already there)
echo [*] Checking dependencies...
call ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip >nul 2>&1
call ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo [!] Failed to install dependencies.
    pause
    exit /b 1
)

REM 5. Run the bot
echo.
echo [*] Bot is starting... When you see "Bot started" the bot is online.
echo [*] Keep this window open. Close it (or press Ctrl+C) to stop the bot.
echo.
".venv\Scripts\python.exe" main.py

echo.
echo Bot stopped. Press any key to close this window.
pause >nul
