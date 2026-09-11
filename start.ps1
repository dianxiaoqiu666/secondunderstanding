param([int]$UiPort = 8130, [int]$PlanningPort = 8132, [string]$UnderstandingUrl = 'http://127.0.0.1:8131')
$ErrorActionPreference = 'Stop'
$shelfPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $shelfPython)) {
    throw 'Local environment is missing. Run scripts/setup.ps1 first.'
}
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
& $shelfPython -B (Join-Path $PSScriptRoot 'scripts\start.py') --ui-port $UiPort --planning-port $PlanningPort --understanding-url $UnderstandingUrl
exit $LASTEXITCODE
