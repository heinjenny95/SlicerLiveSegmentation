[CmdletBinding()]
param(
    [string]$SlicerPath = '',
    [switch]$NoPopup
)

$ErrorActionPreference = 'Stop'
$packageRoot = $PSScriptRoot
$moduleSource = Join-Path $packageRoot 'LiveSegmentation'
$version = (Get-Content -LiteralPath (Join-Path $packageRoot 'VERSION') -Raw).Trim()

if (-not (Test-Path -LiteralPath (Join-Path $moduleSource 'LiveSegmentation.py'))) {
    throw "LiveSegmentation.py wurde im entpackten Paket nicht gefunden: $moduleSource"
}

if (-not $SlicerPath) {
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

$SlicerPath = (Resolve-Path -LiteralPath $SlicerPath).Path
$documents = [Environment]::GetFolderPath('MyDocuments')
$moduleDestination = Join-Path $documents "SlicerExtensions\LiveSegmentation-$version"
New-Item -ItemType Directory -Path $moduleDestination -Force | Out-Null

Get-ChildItem -LiteralPath $moduleSource -Recurse -File |
    Where-Object {
        $_.Extension -ne '.pyc' -and $_.FullName -notmatch '[\\/]__pycache__[\\/]'
    } |
    ForEach-Object {
    $relative = $_.FullName.Substring($moduleSource.Length + 1)
    $target = Join-Path $moduleDestination $relative
    $targetParent = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $targetParent)) {
        New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
    }
    Copy-Item -LiteralPath $_.FullName -Destination $target -Force
}

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Live Segmentation.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $SlicerPath
$shortcut.Arguments = "--additional-module-path `"$moduleDestination`" --python-code `"slicer.util.selectModule('LiveSegmentation')`""
$shortcut.WorkingDirectory = Split-Path -Parent $SlicerPath
$shortcut.IconLocation = "$SlicerPath,0"
$shortcut.Description = '3D Slicer mit dem eigenständigen Live Segmentation Plugin öffnen'
$shortcut.Save()

$safeShortcutPath = Join-Path $desktop 'Live Segmentation Safe Start.lnk'
$safeShortcut = $shell.CreateShortcut($safeShortcutPath)
$safeShortcut.TargetPath = $SlicerPath
$safeShortcut.Arguments = "--disable-settings --ignore-slicerrc --additional-module-path `"$moduleDestination`" --python-code `"slicer.util.selectModule('LiveSegmentation')`""
$safeShortcut.WorkingDirectory = Split-Path -Parent $SlicerPath
$safeShortcut.IconLocation = "$SlicerPath,0"
$safeShortcut.Description = 'Live Segmentation mit isolierten lokalen Slicer-Einstellungen starten'
$safeShortcut.Save()

$roomScript = Join-Path $moduleDestination 'Resources\Scripts\OpenLiveSegmentationRoom.py'
$classesRoot = 'HKCU:\Software\Classes'
$extensionKey = Join-Path $classesRoot '.livesegroom'
$classKey = Join-Path $classesRoot 'LiveSegmentation.Room'
$iconKey = Join-Path $classKey 'DefaultIcon'
$commandKey = Join-Path $classKey 'shell\open\command'
New-Item -Path $extensionKey -Force | Out-Null
Set-Item -Path $extensionKey -Value 'LiveSegmentation.Room'
New-Item -Path $classKey -Force | Out-Null
Set-Item -Path $classKey -Value 'Live Segmentation room invitation'
New-Item -Path $iconKey -Force | Out-Null
Set-Item -Path $iconKey -Value "$SlicerPath,0"
New-Item -Path $commandKey -Force | Out-Null
$openRoomCommand = "`"$SlicerPath`" --additional-module-path `"$moduleDestination`" --python-script `"$roomScript`" `"%1`""
Set-Item -Path $commandKey -Value $openRoomCommand

$message = @"
Live Segmentation $version wurde separat installiert.

Andere Segmentierungs-Plugins: bleiben unverändert installiert
Live-Plugin: $moduleDestination
Desktop-Shortcut: $shortcutPath
Recovery-Shortcut ohne gespeicherte Slicer-Einstellungen: $safeShortcutPath
Einladungen: .livesegroom-Dateien öffnen Slicer und laden den Raum automatisch

Öffne künftig den neuen Shortcut „Live Segmentation“.
Falls Slicer mit alten Einstellungen nicht startet, verwende einmal
„Live Segmentation Safe Start“.
"@

Write-Host $message
if (-not $NoPopup) {
    $null = $shell.Popup($message, 0, 'Live Segmentation installiert', 64)
}
