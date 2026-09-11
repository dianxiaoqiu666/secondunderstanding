$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:TEMP = Join-Path $PSScriptRoot '.tmp'
$env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$pythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Local dependencies are missing. Run: powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1'
}
& $pythonPath -B scripts/start.py
exit $LASTEXITCODE
