$ErrorActionPreference = 'Stop'
$shelfPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $shelfPython)) { throw 'Local Python environment is missing.' }
Set-Location -LiteralPath $PSScriptRoot
& $shelfPython -B (Join-Path $PSScriptRoot 'scripts\start.py') --stop
exit $LASTEXITCODE
