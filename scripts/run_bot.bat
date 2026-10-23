@echo off
cd /d "%~dp0.."
echo Starting Discord Bot...
.venv\Scripts\python.exe discord_bot.py
pause
