param(
    [string]$SlicerPath
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$modulePath = Join-Path $projectRoot 'LiveSegmentation'

function Show-LauncherError([string]$Message) {
    $shell = New-Object -ComObject WScript.Shell
    $null = $shell.Popup($Message, 0, 'Live Segmentation', 16)
}

try {
    if (-not $SlicerPath) {
        # Same discovery as Install-LiveSegmentation.ps1: newest per-user Slicer.
        $slicerRoot = Join-Path $env:LOCALAPPDATA 'slicer.org'
        $candidates = @(
            Get-ChildItem -LiteralPath $slicerRoot -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { Join-Path $_.FullName 'Slicer.exe' } |
                Where-Object { Test-Path -LiteralPath $_ } |
                Sort-Object { (Get-Item -LiteralPath $_).LastWriteTime } -Descending
        )
        if (-not $candidates) {
            throw '3D Slicer wurde nicht gefunden. Installiere Slicer oder starte dieses Skript mit -SlicerPath.'
        }
        $SlicerPath = $candidates[0]
    }
    if (-not (Test-Path -LiteralPath $SlicerPath)) {
        throw "3D Slicer wurde nicht gefunden: $SlicerPath"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $modulePath 'LiveSegmentation.py'))) {
        throw "Das Live-Segmentation-Modul wurde nicht gefunden: $modulePath"
    }

    Start-Process `
        -FilePath $SlicerPath `
        -ArgumentList @(
            '--additional-module-path',
            $modulePath,
            '--python-code',
            "slicer.util.selectModule('LiveSegmentation')"
        ) `
        -WorkingDirectory (Split-Path -Parent $SlicerPath)
} catch {
    Show-LauncherError $_.Exception.Message
    exit 1
}
