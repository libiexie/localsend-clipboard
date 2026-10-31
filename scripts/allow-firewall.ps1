# Allow inbound TCP 53318 for the LocalSend Clipboard receiver (one-time, needs admin).
# ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 files as ANSI when they
# have no BOM, so non-ASCII text would break parsing.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File allow-firewall.ps1
#   powershell ... -File allow-firewall.ps1 -PythonPath "C:\Python312\python.exe" -Port 53318
# Check only (no changes, no UAC):
#   powershell ... -File allow-firewall.ps1 -CheckOnly
#
# Why: by default Windows Firewall blocks inbound connections. The phone must
# reach the receiver's TCP port on this PC. The rule is scoped to the given
# program, not the whole machine.

param(
    [string]$PythonPath = "",
    [int]$Port = 53318,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$ruleName = "LocalSend Clipboard Receiver"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not $PythonPath) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $PythonPath = $cmd.Source }
}
if (-not $PythonPath -or -not (Test-Path $PythonPath)) {
    Write-Host "ERROR: python.exe not found. Pass -PythonPath C:\path\to\python.exe" -ForegroundColor Red
    exit 1
}
$PythonPath = (Resolve-Path $PythonPath).Path

Write-Host "LocalSend Clipboard Receiver - firewall rule"
Write-Host "  program: $PythonPath"
Write-Host "  port   : TCP $Port"
Write-Host ""

if ($CheckOnly) {
    Write-Host "CheckOnly mode: nothing changed. Run without -CheckOnly to apply."
    exit 0
}

if (-not (Test-Admin)) {
    Write-Host "Not elevated - relaunching with UAC prompt..."
    $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"{0}`" -PythonPath `"{1}`" -Port {2}" -f $PSCommandPath, $PythonPath, $Port
    Start-Process -FilePath "powershell.exe" -ArgumentList $argLine -Verb RunAs
    Write-Host "Accept the UAC prompt to continue (this window can be closed)."
    exit 0
}

$existing = netsh advfirewall firewall show rule name="$ruleName"
if ($LASTEXITCODE -eq 0 -and $existing -match "Rule Name") {
    netsh advfirewall firewall delete rule name="$ruleName" | Out-Null
}

netsh advfirewall firewall add rule name="$ruleName" dir=in action=allow protocol=TCP `
    localport=$Port program="$PythonPath" profile=any enable=yes | Out-Null

Write-Host "DONE. Rule '$ruleName' now allows inbound TCP $Port for:"
Write-Host "  $PythonPath"
Write-Host ""
Write-Host "Remove later with:"
Write-Host "  netsh advfirewall firewall delete rule name=\"$ruleName\""
Write-Host ""
Read-Host "Press Enter to close"
