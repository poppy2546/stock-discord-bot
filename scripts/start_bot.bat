@echo off
cd /d "%~dp0.."
powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '.\.venv\Scripts\pythonw.exe' -ArgumentList 'discord_bot.py' -WorkingDirectory (Get-Location).Path -WindowStyle Hidden"
exit
