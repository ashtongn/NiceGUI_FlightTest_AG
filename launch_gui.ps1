<#
launch_gui.ps1: MOTHBALL FURY ground-station GUI, natively on Windows.

    .\launch_gui.ps1                  GUI on http://localhost:8080 (Ctrl-C stops)
    .\launch_gui.ps1 -Port 8081       alternate TCP port
    .\launch_gui.ps1 -Port 8081 -DiscoveryPort 47841
                                      ...and an alternate UDP discovery port, so
                                      this GUI can run beside one holding 47811

Creates/activates .venv next to this file, installs requirements-gui.txt (the
GUI set: no posix_ipc, no libsidekiq; the ground station never loads the radio
library) and runs run_gui_link.py. Mirrors launch_gui.sh --native.

If scripts are blocked:  powershell -ExecutionPolicy Bypass -File .\launch_gui.ps1

Windows Firewall must allow inbound UDP to this venv's python.exe (ANNOUNCE on
47811 + telemetry replies on the GUI's session port); see the "Windows ground
station" section of DPMG_User_Manual.md / docs/QUICKSTART.md.
#>
param(
    [int]$Port = 8080,
    [int]$DiscoveryPort = 0
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$url = "http://localhost:$Port"

# --- find a Python 3 interpreter (py launcher first, then python) -------------
$py = $null
foreach ($cand in @(@("py", "-3"), @("python"))) {
    try {
        & $cand[0] $cand[1..($cand.Length-1)] -c "import sys; assert sys.version_info >= (3, 10)" 2>$null
        if ($LASTEXITCODE -eq 0) { $py = $cand; break }
    } catch { }
}
if (-not $py) {
    Write-Error "no Python 3.10+ found. Install Python 3.11+ from python.org and tick 'Add to PATH'."
    exit 1
}

# --- venv ----------------------------------------------------------------------
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "creating virtual environment .venv"
    & $py[0] $py[1..($py.Length-1)] -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Error "venv creation failed"; exit 1 }
}
. ".venv\Scripts\Activate.ps1"

Write-Host "installing GUI requirements (requirements-gui.txt)"
python -m pip install -q --disable-pip-version-check -r requirements-gui.txt
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed. Is the laptop online, or is a wheel cache configured?"
    exit 1
}

# --- open the browser once the GUI answers (background job) -----------------
$opener = Start-Job -ArgumentList $url -ScriptBlock {
    param($u)
    for ($i = 0; $i -lt 120; $i++) {
        try {
            Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 2 | Out-Null
            Start-Process $u
            return "opened $u"
        } catch { Start-Sleep -Seconds 1 }
    }
    "WARNING: GUI did not answer at $u within 120 s"
}

Write-Host "starting native GUI on $url (Ctrl-C to stop)"
try {
    if ($DiscoveryPort -gt 0) {
        Write-Host "discovery port $DiscoveryPort"
        python run_gui_link.py $Port $DiscoveryPort
    } else {
        python run_gui_link.py $Port
    }
} finally {
    Receive-Job $opener -ErrorAction SilentlyContinue | Write-Host
    Remove-Job $opener -Force -ErrorAction SilentlyContinue
}
