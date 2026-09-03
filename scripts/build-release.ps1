$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw 'Run scripts\setup.ps1 first.'
}

& $venvPython -m ruff check (Join-Path $projectRoot 'server') (Join-Path $projectRoot 'LiveSegmentation') (Join-Path $projectRoot 'scripts')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& (Join-Path $projectRoot 'scripts\test.ps1')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $venvPython (Join-Path $projectRoot 'scripts\build_release.py') --output (Join-Path $projectRoot 'dist')
exit $LASTEXITCODE
