$projectRoot = Split-Path -Parent $PSScriptRoot
$batPath = Join-Path $PSScriptRoot "run_scan.bat"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$batPath`""

# Trigger 1: Summary Phase at 07:00 AM (Daily close confirmation & PnL summary)
$triggerSummary = New-ScheduledTaskTrigger -Daily -At "07:00"

# Trigger 2: Planning Phase at 20:00 PM (Pre-market radar & order preparation)
$triggerPlan = New-ScheduledTaskTrigger -Daily -At "20:00"

# Trigger 3: Trading Phase at 23:00 PM (Market active trading signal)
$triggerTrade = New-ScheduledTaskTrigger -Daily -At "23:00"

Register-ScheduledTask -TaskName "StockAnalysisDaily" -Action $action -Trigger @($triggerSummary, $triggerPlan, $triggerTrade) -Description "Daily Stock Screening (3 Phases: 07:00 Summary, 20:00 Planning, 23:00 Trading)" -Force

# Remove obsolete 22:00 task
Unregister-ScheduledTask -TaskName "StockAnalysisDaily22PM" -Confirm:$false -ErrorAction SilentlyContinue
