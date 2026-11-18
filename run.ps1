# Start the Gemini Enterprise bridge (if needed) and open OpenCode on a project.
# No stored credentials: if you need to sign in, a browser window opens; log in and it closes itself.
#   hydrogen-manual                        -> OpenCode in the current folder
#   hydrogen-manual C:\path\to\project     -> OpenCode in that folder
#   hydrogen-manual -BridgeOnly            -> just run the bridge (for other tools)
#   hydrogen-manual -ShowBrowser           -> keep the bridge's browser window visible the whole time
#   hydrogen-manual -Model claude-opus-5-5 -> start OpenCode on Opus 5.5 (switch any time with /models)
param(
    [string]$Project = (Get-Location).Path,
    [switch]$BridgeOnly,
    [switch]$ShowBrowser,
    [string]$Model = "gemini-4-hydrogen",
    [int]$Port = 8766
)

$base = "http://127.0.0.1:$Port/v1"
function Test-Bridge { try { Invoke-RestMethod "$base/models" -TimeoutSec 2 | Out-Null; $true } catch { $false } }

$bridge = $null
if (Test-Bridge) {
    Write-Host "Bridge already running at $base"
} else {
    $env:PORT = "$Port"; $env:MODEL = $Model
    $env:HEADLESS = if ($ShowBrowser) { "" } else { "1" }
    $bridge = Start-Process python -ArgumentList "-u", "`"$PSScriptRoot\bridge.py`"" -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput "$PSScriptRoot\bridge.log" -RedirectStandardError "$PSScriptRoot\bridge.err.log"
    Write-Host "Starting bridge... if a browser window opens, sign in there (it closes when you're done)."
    $deadline = (Get-Date).AddSeconds(660)
    while (-not (Test-Bridge)) {
        if ($bridge.HasExited -or (Get-Date) -gt $deadline) {
            Write-Error "Bridge failed to start. See $PSScriptRoot\bridge.err.log"; exit 1
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "Bridge ready at $base"
}

if ($BridgeOnly) {
    Write-Host "Base URL: $base   API key: x   Model: $Model"
    if ($bridge) { Write-Host "Press Ctrl+C to stop."; Wait-Process -Id $bridge.Id }
    exit
}

# OpenCode is a Node package; make sure node and global npm bins are reachable.
$env:Path = "C:\Program Files\nodejs;$env:APPDATA\npm;$env:Path"
$env:OPENCODE_CONFIG = "$PSScriptRoot\opencode.json"

Push-Location $Project
try {
    opencode --model "hydrogen/$Model"
} finally {
    Pop-Location
    if ($bridge -and -not $bridge.HasExited) { Stop-Process -Id $bridge.Id }
}
