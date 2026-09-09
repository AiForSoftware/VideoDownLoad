# Startup memory probe for 全能下载器 (VideoDLDesktop)
# Only counts processes that belong to THIS app:
#   * VideoDLDesktop.exe      (supervisor + UI child)
#   * msedgewebview2.exe      whose command line carries our vd-desktop user-data folder
#   * msedge/chrome.exe       launched by DrissionPage (--remote-debugging-port=)
# It waits until startup.log reports "webview page loaded" (so we measure the STEADY
# state, after the working-set trim) instead of guessing a fixed delay.
# Usage: powershell -ExecutionPolicy Bypass -File app\tools\measure_memory.ps1 [-ExePath <path>] [-NoLaunch]
param(
    [string]$ExePath = 'D:\CodeBuddy\VideoDownLoad\dist\VideoDLDesktop\VideoDLDesktop.exe',
    [string]$ExtraArgs = '',
    [switch]$NoLaunch
)

$ErrorActionPreference = 'SilentlyContinue'
$log = Join-Path $env:LOCALAPPDATA 'vd\vd-desktop\Logs\startup.log'

function Get-Pids($name, $pattern) {
    $out = @()
    foreach ($p in (Get-CimInstance Win32_Process -Filter "Name = '$name'")) {
        if ($p.CommandLine -and $p.CommandLine -like "*$pattern*") { $out += $p.ProcessId }
    }
    return $out
}

function Show-Group($label, $pids) {
    if (-not $pids -or $pids.Count -eq 0) { Write-Host ('{0,-34} count=0' -f $label); return 0 }
    $procs = @(Get-Process -Id $pids)
    if ($procs.Count -eq 0) { Write-Host ('{0,-34} count=0' -f $label); return 0 }
    $ws = [math]::Round((($procs | Measure-Object -Property WorkingSet64 -Sum).Sum) / 1MB, 1)
    $pv = [math]::Round((($procs | Measure-Object -Property PrivateMemorySize64 -Sum).Sum) / 1MB, 1)
    Write-Host ('{0,-34} count={1,-3} WS={2,8} MB   Private={3,8} MB' -f $label, $procs.Count, $ws, $pv)
    foreach ($p in ($procs | Sort-Object -Property WorkingSet64 -Descending)) {
        Write-Host ('    pid={0,-7} WS={1,8} MB  Private={2,8} MB' -f $p.Id, [math]::Round($p.WorkingSet64 / 1MB, 1), [math]::Round($p.PrivateMemorySize64 / 1MB, 1))
    }
    return $ws
}

if (-not $NoLaunch) {
    Write-Host "[1/5] killing any running instance..."
    Get-Process VideoDLDesktop | Stop-Process -Force
    Start-Sleep -Seconds 2
    Write-Host "[2/5] killing leftover WebView2 processes of this app..."
    $k = 0
    foreach ($p in (Get-CimInstance Win32_Process -Filter "Name = 'msedgewebview2.exe'")) {
        if ($p.CommandLine -and $p.CommandLine -like '*vd-desktop*') { Stop-Process -Id $p.ProcessId -Force; $k += 1 }
    }
    Write-Host "      killed $k leftover webview2 process(es)"
    Start-Sleep -Seconds 2
    $mark = 0
    if (Test-Path $log) { $mark = (Get-Item $log).Length }
    if ($ExtraArgs) { $env:VD_WEBVIEW_EXTRA_ARGS = $ExtraArgs; Write-Host "[3/5] extra webview args: $ExtraArgs" }
    else { Remove-Item Env:VD_WEBVIEW_EXTRA_ARGS }
    Write-Host "[3/5] launching $ExePath"
    Start-Process -FilePath $ExePath
    Write-Host "[4/5] waiting for the page to load (startup.log)..."
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 3
        if (Test-Path $log) {
            $len = (Get-Item $log).Length
            if ($len -lt $mark) { $mark = 0 }   # log rotated
            if ($len -gt $mark) {
                # read ONLY the bytes appended after we launched — a plain -Tail would
                # happily match a "page loaded" line left by the previous run and
                # sample while the new instance is still initializing.
                $fs = [System.IO.File]::Open($log, 'Open', 'Read', 'ReadWrite')
                $fs.Position = $mark
                $sr = New-Object System.IO.StreamReader($fs)
                $new = $sr.ReadToEnd()
                $sr.Close(); $fs.Close()
                if ($new -match 'webview page loaded') { $ok = $true; break }
            }
        }
    }
    if (-not $ok) { Write-Host "      WARN: page-loaded never seen; measuring anyway" }
    Write-Host "[5/5] settling 8s (working-set trim runs 2s after load)..."
    Start-Sleep -Seconds 8
}
else {
    Write-Host "[skip] measuring the already-running instance"
}

Write-Host ""
Write-Host "===== startup memory snapshot (this app only) ====="
$appWs = Show-Group 'VideoDLDesktop.exe (app)' (Get-Process VideoDLDesktop | ForEach-Object { $_.Id })
$wvWs = Show-Group 'msedgewebview2.exe (ours)' (Get-Pids 'msedgewebview2.exe' 'vd-desktop')
$dpWs = Show-Group 'msedge/chrome.exe (drission)' ((Get-Pids 'msedge.exe' '--remote-debugging-port=') + (Get-Pids 'chrome.exe' '--remote-debugging-port='))
Write-Host ('TOTAL THIS APP  WS = {0} MB' -f [math]::Round($appWs + $wvWs + $dpWs, 1))
Write-Host ""
Write-Host "DONE"
