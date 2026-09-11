param([switch]$WithBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
New-Item -ItemType Directory -Force .tmp,.cache | Out-Null
$env:TEMP = Join-Path $projectRoot '.tmp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $projectRoot '.cache\pip'
$env:npm_config_cache = Join-Path $projectRoot '.cache\npm'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $projectRoot '.cache\ms-playwright'
if (-not (Test-Path .venv\Scripts\python.exe)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
}
& .venv\Scripts\python.exe -m pip install --requirement requirements.lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed' }
# The pinned, licensed Three.js runtime is vendored. No Node/npm is needed to run.
if ($WithBrowser) {
    & .venv\Scripts\python.exe -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Browser installation failed' }
}
Write-Output 'Setup complete. Start offline with: powershell -ExecutionPolicy Bypass -File .\start.ps1'
