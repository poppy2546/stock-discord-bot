@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo ==========================================
echo Stopping Discord Bot (Background)...
echo ==========================================
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'python*.exe') -and ($_.CommandLine -like '*discord_bot.py*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host 'Stopped PID:' $_.ProcessId }"
echo ------------------------------------------
echo Done.
ping -n 2 127.0.0.1 >nul
exit
