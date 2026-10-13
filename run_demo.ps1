# ============================================================================
#  run_demo.ps1 - one-command demo for the predictive-maintenance project
#
#  What it does (no third-party dependencies needed):
#    1. start the Modbus TCP slave simulator (with fault injection)
#    2. collect data for N seconds into SQLite (auto-reconnect enabled)
#    3. run the analysis layer and generate docs/report.html
#    4. generate the labeled dataset for the modeling scripts
#
#  Usage:
#    powershell -ExecutionPolicy Bypass -File run_demo.ps1 -CollectSeconds 30
#    pwsh -File run_demo.ps1 -CollectSeconds 30        # PowerShell 7 works too
#
#  Notes:
#    * Messages are ASCII on purpose: the Windows console default codepage is GBK,
#      and non-ASCII output can break older shells. The Python scripts force UTF-8
#      on their own stdout, so their Chinese log lines never crash.
#    * Start-BackgroundProcess() prefers Start-Process but falls back to
#      "cmd /c start /b" when Start-Process is not usable. On some launchers the
#      child environment contains BOTH "NO_PROXY" and "no_proxy"; PowerShell 7
#      builds a case-insensitive dictionary from it and Start-Process then throws
#      "An item with the same key has already been added". The fallback keeps this
#      script working in that environment instead of failing.
#    * The simulator is stopped by the PID that owns the listening port, so it
#      works no matter which launch path was used.
# ============================================================================
param(
    [int]$CollectSeconds = 60,
    [int]$FaultAfter     = 15,
    [int]$Port           = 5020
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

New-Item -ItemType Directory -Force -Path 'logs', 'data', 'docs', 'models' | Out-Null
Remove-Item 'data\sensor.db' -Force -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- helpers
function Start-BackgroundProcess {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$OutFile,
        [string]$ErrFile
    )
    try {
        if ($OutFile) {
            return Start-Process -FilePath $FilePath -ArgumentList $Arguments -PassThru `
                -WindowStyle Hidden -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile
        }
        return Start-Process -FilePath $FilePath -ArgumentList $Arguments -PassThru -WindowStyle Hidden
    } catch {
        Write-Host ("      [warn] Start-Process unavailable (" + $_.Exception.GetType().Name +
                    "); falling back to 'cmd /c start /b'")
        $quoted = ($Arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
        $redir = ''
        if ($OutFile) { $redir = ' > "' + $OutFile + '" 2> "' + $ErrFile + '"' }
        & cmd.exe /c ('start "" /b "' + $FilePath + '" ' + $quoted + $redir) | Out-Null
        return $null
    }
}

function Stop-ProcessOnPort {
    param([int]$PortNumber)
    $conns = @(Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction SilentlyContinue)
    foreach ($c in $conns) {
        try {
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
            Write-Host ("      stopped PID " + $c.OwningProcess + " (was listening on " + $PortNumber + ")")
        } catch { }
    }
}

# ---------------------------------------------------------------- find python
$candidates = @()
$uvPy = Join-Path $env:APPDATA 'uv\python'
if (Test-Path $uvPy) {
    $candidates += (Get-ChildItem $uvPy -Recurse -Filter 'python.exe' -ErrorAction SilentlyContinue |
                    Where-Object { $_.FullName -match 'cpython-3\.12' } |
                    Select-Object -First 1 -ExpandProperty FullName)
}
$onPath = Get-Command python -ErrorAction SilentlyContinue
if ($onPath -and $onPath.Source -notmatch 'WindowsApps') { $candidates += $onPath.Source }
$py = $candidates | Where-Object { $_ } | Select-Object -First 1

if (-not $py) {
    Write-Host '[ERROR] No usable Python found.'
    Write-Host '        Install Python 3.10+ , or install uv (https://docs.astral.sh/uv/)'
    Write-Host '        Note: the "python.exe" from Microsoft Store is a stub and will not work.'
    exit 1
}
Write-Host ("[env] python = " + $py)
Write-Host ("[env] " + (& $py --version 2>&1))
Write-Host ("[env] powershell = " + $PSVersionTable.PSVersion.ToString())

# ---------------------------------------------------------------- syntax check
Write-Host ''
Write-Host '[1/4] syntax check ...'
& $py -m py_compile modbus_sim.py collector.py analyze.py make_dataset.py
if ($LASTEXITCODE -ne 0) { Write-Host '[ERROR] syntax error, aborting.'; exit 1 }
Write-Host '      OK'

# ---------------------------------------------------------------- start slave
Write-Host ''
Write-Host ("[2/4] starting Modbus TCP slave on 127.0.0.1:" + $Port + " (degradation after " + $FaultAfter + "s) ...")
$null = Start-BackgroundProcess -FilePath $py `
    -Arguments @('modbus_sim.py', '--port', "$Port", '--fault-after', "$FaultAfter") `
    -OutFile 'logs\sim.out' -ErrFile 'logs\sim.err'

$listening = $false
for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Milliseconds 400
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { $listening = $true; break }
}
Write-Host ("      listening = " + $listening)
if (-not $listening) { Write-Host '[WARN] slave did not bind; collection will still run to show reconnect behavior.' }

# ---------------------------------------------------------------- collect
Write-Host ''
Write-Host ("[3/4] collecting for " + $CollectSeconds + "s ...")
& $py collector.py --duration $CollectSeconds --interval 1 --db data/sensor.db

Stop-ProcessOnPort -PortNumber $Port
Start-Sleep -Seconds 1

# ---------------------------------------------------------------- analyze
Write-Host ''
Write-Host '[4/4] analysis + dataset ...'
& $py analyze.py --db data/sensor.db --out docs/report.html
& $py make_dataset.py --n 2000 --fault-ratio 0.3 --out data/dataset.csv

Write-Host ''
Write-Host '==================== DONE ===================='
Write-Host (" report   : " + (Resolve-Path 'docs\report.html'))
Write-Host (" dataset  : " + (Resolve-Path 'data\dataset.csv'))
Write-Host (" database : " + (Resolve-Path 'data\sensor.db'))
Write-Host ''
Write-Host ' Next (modeling, needs numpy + scikit-learn):'
Write-Host '   uv run --python 3.12 --with scikit-learn --with numpy python train_baseline.py'
Write-Host '   uv run --python 3.12 --with scikit-learn --with numpy python train_rul.py'
Write-Host '============================================='
