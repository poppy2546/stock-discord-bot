$scriptDir = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $scriptDir ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    $pythonExe = $venvPy
} else {
    $pythonExe = "python"
}

Set-Location -LiteralPath $scriptDir
& $pythonExe (Join-Path $scriptDir "scan_stocks.py")
