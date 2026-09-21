param([int]$Port = 8765, [string]$PythonPath)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ($PythonPath -and (Test-Path -LiteralPath $PythonPath)) { $Python = $PythonPath }
elseif ($env:TAICAI_PYTHON -and (Test-Path -LiteralPath $env:TAICAI_PYTHON)) { $Python = $env:TAICAI_PYTHON }
else { $Python = (Get-Command python -ErrorAction Stop).Source }
Write-Output "Dashboard: http://127.0.0.1:$Port"
Push-Location $ProjectRoot
try { & $Python -m outputs.tai_cai_tracker.dashboard --ledger-path outputs/tai_cai_tracker/data/paper_ledger.jsonl --scheduler-log-path work/logs/scheduler.jsonl --port $Port }
finally { Pop-Location }
