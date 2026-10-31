# Build a standalone single-file LocalSendClipboard.exe with PyInstaller.
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-exe.ps1
#   powershell ... -File scripts\build-exe.ps1 -Python "C:\Python312\python.exe"
#
# Requirements: Python 3.10+ with pip; this script installs requirements-dev.txt.

param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "== 1) Install build dependencies =="
& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "== 2) Generate application icon =="
New-Item -ItemType Directory -Force -Path "build" | Out-Null
$iconScript = @'
import os
import sys
sys.path.insert(0, os.getcwd())
from app import make_icon
from PIL import Image

os.makedirs("build", exist_ok=True)
base = make_icon().resize((256, 256), Image.LANCZOS)
im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
im.alpha_composite(base)
im.save(os.path.join("build", "app.ico"),
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon written to build/app.ico")
'@
$iconScript | Out-File -Encoding utf8 "build\_mk_icon.py"
& $Python "build\_mk_icon.py"
if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }

Write-Host "== 3) PyInstaller build =="
& $Python -m PyInstaller --noconfirm --clean --onefile --noconsole `
    --name LocalSendClipboard `
    --icon "build\app.ico" `
    --version-file "packaging\version_info.txt" `
    --collect-all pillow_heif `
    --collect-all pystray `
    --collect-submodules cryptography `
    --hidden-import PIL `
    --distpath "dist" `
    --workpath "build\pyinstaller" `
    --specpath "build" `
    "app.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "== Done =="
Get-Item "dist\LocalSendClipboard.exe" | Select-Object FullName, Length, LastWriteTime | Format-List
Write-Host ("SHA-256: " + (Get-FileHash "dist\LocalSendClipboard.exe" -Algorithm SHA256).Hash.ToLower())
