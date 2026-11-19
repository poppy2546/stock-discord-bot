@echo off
cd /d "%~dp0.."
powershell -NoProfile -WindowStyle Hidden -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'python*.exe') -and ($_.CommandLine -like '*discord_bot.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Process -FilePath '.\.venv\Scripts\pythonw.exe' -ArgumentList 'discord_bot.py' -WorkingDirectory (Get-Location).Path -WindowStyle Hidden"
exit
