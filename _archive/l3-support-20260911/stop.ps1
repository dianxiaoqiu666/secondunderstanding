$ErrorActionPreference = 'Stop'
$runtimePath = Join-Path $PSScriptRoot 'runtime'
if (Test-Path -LiteralPath $runtimePath) {
    Set-Content -LiteralPath (Join-Path $runtimePath 'stop.request') -Value 'stop' -Encoding utf8
    Write-Output 'Stop requested. The launcher will stop its three child services.'
}
