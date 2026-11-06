@echo off
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)
if not exist .env copy .env.example .env
.venv\Scripts\python.exe bot.py
pause
