# SUI-Quant OBR-SUI APK Build & Auto-Increment Release Script
# Target Output: D:\OBR-SUI\Release\OBR-SUI V{major}.{minor}.apk

$ErrorActionPreference = "Stop"

$androidDir = Join-Path $PSScriptRoot "android"
$releaseDir = Join-Path $PSScriptRoot "Release"

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Starting OBR-SUI APK Build & Release Process... " -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

Push-Location $androidDir
try {
    & ".\gradlew.bat" assembleDebug
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Gradle build failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Write-Host "`nRelease Directory Content:" -ForegroundColor Green
Get-ChildItem -Path $releaseDir | Format-Table Name, Length, LastWriteTime
