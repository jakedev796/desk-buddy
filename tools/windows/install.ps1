# Install (or update) the Desk Buddy bridge on Windows: the Windows counterpart of tools/install_bridge.sh.
#   powershell -ExecutionPolicy Bypass -File tools\windows\install.ps1
#   powershell -ExecutionPolicy Bypass -File tools\windows\install.ps1 -Uninstall
# Runtime lives in ~/.desk-buddy (venv\, bridge.py, keepalive.py, sounds\, bridge.log), as on the Mac.
# It starts at logon from a shortcut in your Startup folder (no admin rights needed; it shows under
# Task Manager > Startup apps), running keepalive.py with pythonw.exe so there's no window, which
# restarts the bridge if it ever stops. It also installs Lump Control (control.py), a tray app to test
# Lump and change his settings, started at logon the same way.
# Then hook up Claude Code with: ~\.desk-buddy\venv\Scripts\python.exe tools\install_hooks.py
# (the bridge's own Python: the status line keeps working when the system Python is upgraded or removed)
# Uninstall stops both and removes their shortcuts; the runtime folder is left in place.
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$RT = Join-Path $HOME ".desk-buddy"
$Startup = [Environment]::GetFolderPath("Startup")
$Shortcut = Join-Path $Startup "Desk Buddy bridge.lnk"
$ControlShortcut = Join-Path $Startup "Lump Control.lnk"
# Fallbacks where PowerShell can't make shortcuts (Constrained Language Mode, e.g. under AppLocker or WDAC)
$StartupCmd = Join-Path $Startup "Desk Buddy bridge.cmd"
$ControlCmd = Join-Path $Startup "Lump Control.cmd"
$Venv = Join-Path $RT "venv"
$Python = Join-Path $Venv "Scripts\python.exe"

function Stop-Control {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -match '[\\/]\.desk-buddy[\\/]control\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

& (Join-Path $PSScriptRoot "bridge.ps1") -Stop | Out-Null
Stop-Control
if ($Uninstall) {
    Remove-Item $Shortcut, $ControlShortcut, $StartupCmd, $ControlCmd -ErrorAction SilentlyContinue
    Write-Output "Bridge and Lump Control stopped and removed from startup (runtime left in $RT)."
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
# Behind a corporate network that inspects HTTPS, pip and PlatformIO don't know the company's root
# certificate (Windows does). Give them Windows' trust list for this run (see ca_bundle.py).
function Use-WindowsCertificates([string]$Py) {
    $ca = Join-Path (Join-Path $HOME ".desk-buddy") "windows-ca.pem"
    & $Py -I (Join-Path $PSScriptRoot "ca_bundle.py") $ca
    if (-not $LASTEXITCODE) { $env:PIP_CERT = $ca; $env:REQUESTS_CA_BUNDLE = $ca; $env:SSL_CERT_FILE = $ca }
}

Use-WindowsCertificates $Python
Invoke-Checked $Python @("-m", "pip", "-q", "install", "--disable-pip-version-check", "--upgrade", "pip")
Invoke-Checked $Python @("-m", "pip", "-q", "install", "--disable-pip-version-check", "pyserial", "psutil", "icalendar", "recurring-ical-events")
# Lump Control's packages on their own: if one has no wheel for this Python (pywebview needs pythonnet),
# the bridge still gets installed, and Lump Control falls back to its plain Tk window.
& $Python -m pip -q install --disable-pip-version-check pystray pillow pywebview
if ($LASTEXITCODE) { Write-Warning "Lump Control's extras (pystray, pillow, pywebview) didn't install: the bridge is fine; Lump Control may be limited." }
Copy-Item (Join-Path $Root "tools\buddy_bridge.py") (Join-Path $RT "bridge.py") -Force
# Lump Control's window (an HTML page shown in WebView2) and the board's own drawings, which it shows him with
New-Item -ItemType Directory -Force (Join-Path $RT "ui") | Out-Null
Copy-Item (Join-Path $Root "tools\windows\ui\*") (Join-Path $RT "ui") -Force
Copy-Item (Join-Path $Root "firmware\data\lump.bin") (Join-Path $RT "ui\lump.bin") -Force
Remove-Item (Join-Path $RT "lump.ico") -ErrorAction SilentlyContinue      # remade from lump_icon.png on next start
Copy-Item (Join-Path $Root "tools\windows\keepalive.py") (Join-Path $RT "keepalive.py") -Force
Copy-Item (Join-Path $Root "tools\windows\control.py") (Join-Path $RT "control.py") -Force
Copy-Item (Join-Path $Root "tools\windows\bridge.ps1") (Join-Path $RT "bridge.ps1") -Force
Copy-Item (Join-Path $Root "tools\lumpbar\icon.png") (Join-Path $RT "lump_icon.png") -Force
# Lump's voice (and sounds.json, the first time), then the sound effects for his tap gags
Invoke-Checked $Python @("-I", (Join-Path $Root "tools\make_sounds.py"))
Invoke-Checked $Python @("-I", (Join-Path $Root "tools\make_gag_sounds.py"))

Remove-Item $StartupCmd, $ControlCmd -ErrorAction SilentlyContinue
try {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Shortcut)
    $lnk.TargetPath = Join-Path $Venv "Scripts\pythonw.exe"
    $lnk.Arguments = "`"$(Join-Path $RT 'keepalive.py')`""
    $lnk.WorkingDirectory = $RT
    $lnk.Description = "Desk Buddy bridge: Claude -> Lump"
    $lnk.Save()
    $lnk = $shell.CreateShortcut($ControlShortcut)
    $lnk.TargetPath = Join-Path $Venv "Scripts\pythonw.exe"
    $lnk.Arguments = "`"$(Join-Path $RT 'control.py')`""
    $lnk.WorkingDirectory = $RT
    $lnk.Description = "Lump Control: test Lump and change his settings"
    $lnk.Save()
} catch {
    # No COM here (Constrained Language Mode): small .cmd files do the same, with a console that flashes at
    # logon. %USERPROFILE% rather than the path itself, so a name with umlauts survives cmd's code page.
    Write-Warning "Couldn't make Startup shortcuts ($($_.Exception.Message)); using .cmd files instead."
    $py = '"%USERPROFILE%\.desk-buddy\venv\Scripts\pythonw.exe"'
    Set-Content -Path $StartupCmd -Encoding ASCII -Value "@start `"`" /d `"%USERPROFILE%\.desk-buddy`" $py `"%USERPROFILE%\.desk-buddy\keepalive.py`""
    Set-Content -Path $ControlCmd -Encoding ASCII -Value "@start `"`" /d `"%USERPROFILE%\.desk-buddy`" $py `"%USERPROFILE%\.desk-buddy\control.py`""
}

& (Join-Path $PSScriptRoot "bridge.ps1") -Start | Out-Null
Start-Process -FilePath (Join-Path $Venv "Scripts\pythonw.exe") -ArgumentList "`"$(Join-Path $RT 'control.py')`"" -WorkingDirectory $RT
& (Join-Path $PSScriptRoot "bridge.ps1") -Status
Write-Output "Lump Control is in the tray (the ^ by the clock): click Lump to open it."
Write-Output "Installed. It starts by itself when you log in. Next, if you haven't yet: $Python tools\install_hooks.py"
