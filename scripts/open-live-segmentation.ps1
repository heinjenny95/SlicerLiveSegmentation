$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$slicerPath = 'C:\Users\js7541\AppData\Local\slicer.org\3D Slicer 5.12.3\Slicer.exe'
$modulePath = Join-Path $projectRoot 'LiveSegmentation'

function Show-LauncherError([string]$Message) {
    $shell = New-Object -ComObject WScript.Shell
    $null = $shell.Popup($Message, 0, 'Live Segmentation', 16)
}

try {
    if (-not (Test-Path -LiteralPath $slicerPath)) {
        throw "3D Slicer 5.12.3 wurde nicht gefunden: $slicerPath"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $modulePath 'LiveSegmentation.py'))) {
        throw "Das Live-Segmentation-Modul wurde nicht gefunden: $modulePath"
    }

    Start-Process `
        -FilePath $slicerPath `
        -ArgumentList @(
            '--additional-module-path',
            $modulePath,
            '--python-code',
            "slicer.util.selectModule('LiveSegmentation')"
        ) `
        -WorkingDirectory (Split-Path -Parent $slicerPath)
} catch {
    Show-LauncherError $_.Exception.Message
    exit 1
}
