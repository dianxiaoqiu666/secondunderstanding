param([string]$SourceProject, [switch]$WithBrowser)
$ErrorActionPreference = 'Stop'
$shelfRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $shelfRoot
New-Item -ItemType Directory -Force -Path '.tmp','.cache' | Out-Null
$env:TEMP = Join-Path $shelfRoot '.tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $shelfRoot '.cache\pip'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$shelfPython = Join-Path $shelfRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $shelfPython)) {
    if ($SourceProject) {
        & python -X utf8 -B (Join-Path $PSScriptRoot 'setup_from_l3.py') --source $SourceProject
        if ($LASTEXITCODE -ne 0) { throw 'Offline dependency setup failed.' }
    } else {
        & python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'venv creation failed.' }
        & $shelfPython -m pip install --requirement requirements.lock.txt
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    }
}
& $shelfPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
if ($WithBrowser) {
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $shelfRoot '.cache\ms-playwright'
    & $shelfPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Browser installation failed.' }
}
Write-Output 'Ready. Run start.ps1 and open http://127.0.0.1:8130/algorithm'
