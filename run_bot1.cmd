@echo off
rem Launch script for WB Price & Stock Tracker (Project 1).
rem Reads TG_TOKEN from the root .env, sets WB_BOT_TOKEN, runs the bot in demo mode.
cd /d "%~dp0"

for /f "usebackq tokens=1,* delims==" %%a in ("..\.env") do (
    if "%%a"=="TG_TOKEN" set "WB_BOT_TOKEN=%%b"
)
if not defined WB_BOT_TOKEN (
    echo [ERROR] TG_TOKEN not found in ..\.env
    pause
    exit /b 1
)

set "WB_DEMO_MODE=1"
set "PYTHONIOENCODING=utf-8"

rem --- REAL PRICES: to get real Wildberries prices, use a proxy with a clean IP ---
rem 1) set "WB_DEMO_MODE=0"
rem 2) set "WB_PROXY=http://user:pass@host:port"   (http or socks5, e.g. socks5://127.0.0.1:1080 for a local VPN)
rem 3) restart this script. Check access with the /diag command in Telegram.

..\.venv\Scripts\python.exe -u bot.py
