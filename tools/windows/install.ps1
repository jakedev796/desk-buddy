# Install (or update) the Desk Buddy bridge on Windows: the Windows counterpart of tools/install_bridge.sh.
#   powershell -ExecutionPolicy Bypass -File tools\windows\install.ps1
#   powershell -ExecutionPolicy Bypass -File tools\windows\install.ps1 -Uninstall
# Runtime lives in ~/.desk-buddy (venv\, bridge.py, keepalive.py, sounds\, bridge.log), as on the Mac.
# It starts at logon from a shortcut in your Startup folder (no admin rights needed; it shows under
# Task Manager > Startup apps), running keepalive.py with pythonw.exe so there's no window, which
# restarts the bridge if it ever stops. Then hook up Claude Code with: python tools\install_hooks.py
# Uninstall stops it and removes the shortcut; the runtime folder is left in place.
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RT = Join-Path $HOME ".desk-buddy"
$Shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Desk Buddy bridge.lnk"
$Venv = Join-Path $RT "venv"
$Python = Join-Path $Venv "Scripts\python.exe"

& (Join-Path $PSScriptRoot "bridge.ps1") -Stop | Out-Null
if ($Uninstall) {
    Remove-Item $Shortcut -ErrorAction SilentlyContinue
    Write-Output "Bridge stopped and removed from startup (runtime left in $RT)."
    exit 0
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE) { throw "failed ($LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

New-Item -ItemType Directory -Force $RT | Out-Null
if (-not (Test-Path $Python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { Invoke-Checked "py" @("-3", "-m", "venv", $Venv) }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { Invoke-Checked "python" @("-m", "venv", $Venv) }
    else { throw "Python 3 isn't installed: get it from python.org (or the Microsoft Store), then run this again." }
}
Invoke-Checked $Python @("-m", "pip", "-q", "install", "--disable-pip-version-check", "--upgrade", "pip")
Invoke-Checked $Python @("-m", "pip", "-q", "install", "--disable-pip-version-check", "pyserial", "psutil", "icalendar", "recurring-ical-events")
Copy-Item (Join-Path $Root "tools\buddy_bridge.py") (Join-Path $RT "bridge.py") -Force
Copy-Item (Join-Path $Root "tools\windows\keepalive.py") (Join-Path $RT "keepalive.py") -Force
# Lump's voice (and sounds.json, the first time), then the sound effects for his tap gags
Invoke-Checked $Python @("-I", (Join-Path $Root "tools\make_sounds.py"))
Invoke-Checked $Python @("-I", (Join-Path $Root "tools\make_gag_sounds.py"))

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($Shortcut)
$lnk.TargetPath = Join-Path $Venv "Scripts\pythonw.exe"
$lnk.Arguments = "`"$(Join-Path $RT 'keepalive.py')`""
$lnk.WorkingDirectory = $RT
$lnk.Description = "Desk Buddy bridge: Claude -> Lump"
$lnk.Save()

& (Join-Path $PSScriptRoot "bridge.ps1") -Start | Out-Null
& (Join-Path $PSScriptRoot "bridge.ps1") -Status
Write-Output "Installed. It starts by itself when you log in. Next, if you haven't yet: python tools\install_hooks.py"
