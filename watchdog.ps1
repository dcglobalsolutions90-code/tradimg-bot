# Watchdog for kalshi_live.py: restarts it if its HEARTBEAT file goes stale or the process disappears.
# Credentials come from the .env file next to this script (created by `python setup_keys.py`), never from this file.
# Create a file named STOP in this folder to make the watchdog stand down.
$ErrorActionPreference = "Stop"
$Dir = $PSScriptRoot
$HeartbeatPath = Join-Path $Dir "HEARTBEAT"
$StopPath = Join-Path $Dir "STOP"
$StaleSeconds = 90
$CheckIntervalSeconds = 20
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

function Get-BotProcess {
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like "*kalshi_live.py*" }
}

function Restart-Bot($reason) {
    Write-Output "[watchdog] $(Get-Date -Format o) restarting: $reason"
    foreach ($p in (Get-BotProcess)) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    if (Test-Path $StopPath) { Remove-Item $StopPath -Force }
    if (Test-Path $HeartbeatPath) { Remove-Item $HeartbeatPath -Force }
    $env:KALSHI_LIVE = "1"
    $proc = Start-Process -FilePath $Python -ArgumentList "kalshi_live.py --live" -WorkingDirectory $Dir -WindowStyle Hidden -PassThru
    Remove-Item Env:\KALSHI_LIVE -ErrorAction SilentlyContinue
    Write-Output "[watchdog] $(Get-Date -Format o) restarted, new PID $($proc.Id)"
}

Write-Output "[watchdog] $(Get-Date -Format o) started, watching $HeartbeatPath (stale threshold ${StaleSeconds}s)"
while ($true) {
    Start-Sleep -Seconds $CheckIntervalSeconds
    if (Test-Path $StopPath) { continue }
    $procs = Get-BotProcess
    if (-not $procs) { Restart-Bot "bot process not found"; continue }
    if (-not (Test-Path $HeartbeatPath)) {
        $proc = Get-Process -Id $procs[0].ProcessId -ErrorAction SilentlyContinue
        if ($proc -and ((Get-Date) - $proc.StartTime).TotalSeconds -gt $StaleSeconds) { Restart-Bot "no HEARTBEAT file after ${StaleSeconds}s" }
        continue
    }
    $age = ((Get-Date) - (Get-Item $HeartbeatPath).LastWriteTime).TotalSeconds
    if ($age -gt $StaleSeconds) { Restart-Bot "HEARTBEAT stale (${age}s)" }
}
