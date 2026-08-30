param(
    [string]$HostAddress = '127.0.0.1',
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'Run scripts\setup.ps1 first.'
}

& $venvPython -m uvicorn app.main:app --app-dir (Join-Path $projectRoot 'server') --host $HostAddress --port $Port
exit $LASTEXITCODE
