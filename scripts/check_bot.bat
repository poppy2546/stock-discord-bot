@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo ==========================================
echo Checking Discord Bot Status...
echo ==========================================
powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'python*.exe') -and ($_.CommandLine -like '*discord_bot.py*') }; if ($procs) { Write-Host 'STATUS: RUNNING in background' -ForegroundColor Green; $procs | Select-Object ProcessId, ProcessName, CommandLine | Format-Table -AutoSize } else { Write-Host 'STATUS: STOPPED (Not running)' -ForegroundColor Red }"
echo ------------------------------------------
pause
exit
