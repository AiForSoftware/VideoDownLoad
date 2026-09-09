# Show every WebView2 process that belongs to this app: its Chromium --type,
# working set, and whether the memory-saving switches actually reached the process.
param([string]$Match = 'vd-desktop')

$ErrorActionPreference = 'SilentlyContinue'

$rows = foreach ($p in (Get-CimInstance Win32_Process -Filter "Name = 'msedgewebview2.exe'")) {
    if (-not ($p.CommandLine -like "*$Match*")) { continue }
    $type = 'browser'
    if ($p.CommandLine -match '--type=([^\s"]+)') { $type = $Matches[1] }
    $proc = Get-Process -Id $p.ProcessId
    [pscustomobject]@{
        Pid = $p.ProcessId
        Type = $type
        WsMB = [math]::Round($proc.WorkingSet64 / 1MB, 1)
        PrivMB = [math]::Round($proc.PrivateMemorySize64 / 1MB, 1)
        HasDisableGpu = ($p.CommandLine -like '*--disable-gpu*')
        HasJsFlags = ($p.CommandLine -like '*--js-flags*')
    }
}

$rows | Sort-Object -Property WsMB -Descending | Format-Table -AutoSize
Write-Host ('total WS = {0} MB' -f [math]::Round((($rows | Measure-Object -Property WsMB -Sum).Sum), 1))

Write-Host ''
Write-Host '--- app processes ---'
Get-Process VideoDLDesktop | ForEach-Object {
    Write-Host ('pid={0} WS={1} MB Private={2} MB' -f $_.Id, [math]::Round($_.WorkingSet64 / 1MB, 1), [math]::Round($_.PrivateMemorySize64 / 1MB, 1))
}
Write-Host 'DONE'
