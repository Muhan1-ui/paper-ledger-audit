param(
    [ValidateSet("preflight", "prematch", "settle", "status", "cycle")]
    [string]$Mode = "prematch",
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONIOENCODING = "utf-8"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ($PythonPath -and (Test-Path -LiteralPath $PythonPath)) {
    $Python = $PythonPath
} elseif ($env:TAICAI_PYTHON -and (Test-Path -LiteralPath $env:TAICAI_PYTHON)) {
    $Python = $env:TAICAI_PYTHON
} else {
    $Python = (Get-Command python -ErrorAction Stop).Source
}
$Ledger = "outputs\tai_cai_tracker\data\paper_ledger.jsonl"
$LogDirectory = Join-Path $PSScriptRoot "logs"
$PrematchStatePath = Join-Path $LogDirectory "prematch-state.json"
$IdentityReviewQueue = Join-Path $LogDirectory "identity-review-queue.json"
$SchedulerLog = Join-Path $LogDirectory "scheduler.jsonl"
$SalesCutoffPolicy = "work\sales-cutoff-policy.json"
$MutexName = "TaiCaiTracker.PaperLedger"

function Write-SchedulerEntry {
    param([object]$Entry)

    $Entry | ConvertTo-Json -Compress -Depth 8 | Add-Content -Path $SchedulerLog -Encoding utf8
}

function Invoke-TrackerStage {
    param(
        [ValidateSet("preflight", "prematch", "settle", "status")]
        [string]$Stage
    )

    $Output = @()
    $RecoveryScan = $false
    try {
        Push-Location $ProjectRoot
        try {
            if ($Stage -eq "status") {
                $Output = & $Python -m outputs.tai_cai_tracker.cli daily-freeze-status $Ledger $SchedulerLog 2>&1
                if ($LASTEXITCODE -ne 0) {
                    throw "status command failed with exit code $LASTEXITCODE"
                }
            } elseif ($Stage -eq "preflight") {
                $Output = & $Python -m outputs.tai_cai_tracker.cli identity-preflight $IdentityReviewQueue --min-lead-minutes 120 --max-lead-minutes 180 --sales-cutoff-policy $SalesCutoffPolicy 2>&1
                if ($LASTEXITCODE -ne 0) {
                    throw "preflight command failed with exit code $LASTEXITCODE"
                }
            } elseif ($Stage -eq "prematch") {
                $Now = Get-Date
                $MinimumLeadMinutes = 30
                if (Test-Path $PrematchStatePath) {
                    try {
                        $State = Get-Content $PrematchStatePath -Raw | ConvertFrom-Json
                        $LastCompletedAt = [datetime]::Parse($State.last_completed_at)
                        $RecoveryScan = ($Now - $LastCompletedAt).TotalMinutes -gt 90
                    } catch {
                        $RecoveryScan = $true
                    }
                } else {
                    $RecoveryScan = $true
                }
                $Output = & $Python -m outputs.tai_cai_tracker.cli schedule-driven-freeze $Ledger --min-lead-minutes $MinimumLeadMinutes --max-lead-minutes 60 --max-workers 2 --sales-cutoff-policy $SalesCutoffPolicy 2>&1
                if ($LASTEXITCODE -ne 0) {
                    throw "prematch command failed with exit code $LASTEXITCODE"
                }
                [pscustomobject]@{
                    last_completed_at = (Get-Date).ToString("o")
                } | ConvertTo-Json -Compress | Set-Content -Path $PrematchStatePath -Encoding utf8
            } else {
                $SettlementOutput = & $Python -m outputs.tai_cai_tracker.cli sporttery-settle-batch $Ledger 2>&1
                if ($LASTEXITCODE -ne 0) {
                    throw "official settlement command failed with exit code $LASTEXITCODE"
                }
                $Report = & $Python -m outputs.tai_cai_tracker.cli report $Ledger --json 2>&1
                if ($LASTEXITCODE -ne 0) {
                    throw "report command failed with exit code $LASTEXITCODE"
                }
                $PaperBetOutput = & $Python -m outputs.tai_cai_tracker.paper_bet --no-report 2>&1
                if ($LASTEXITCODE -ne 0) {
                    throw "paper bet command failed with exit code $LASTEXITCODE"
                }
                $Output = @($SettlementOutput; $Report; $PaperBetOutput)
            }
        } finally {
            Pop-Location
        }
        $Entry = [pscustomobject]@{
            timestamp = (Get-Date).ToString("o")
            mode = $Stage
            status = "completed"
            recovery_scan = if ($Stage -eq "prematch") { $RecoveryScan } else { $false }
            output = @($Output)
        }
    } catch {
        $Entry = [pscustomobject]@{
            timestamp = (Get-Date).ToString("o")
            mode = $Stage
            status = "failed"
            recovery_scan = if ($Stage -eq "prematch") { $RecoveryScan } else { $false }
            error = $_.Exception.Message
            output = @($Output)
        }
    }
    Write-SchedulerEntry $Entry
    return $Entry
}

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$Mutex = New-Object System.Threading.Mutex($false, $MutexName)
$Acquired = $false

try {
    $Acquired = $Mutex.WaitOne(0)
    if (-not $Acquired) {
        Write-SchedulerEntry ([pscustomobject]@{
            timestamp = (Get-Date).ToString("o")
            mode = $Mode
            status = "skipped_busy"
        })
        exit 0
    }

    if ($Mode -ne "cycle") {
        $Result = Invoke-TrackerStage $Mode
        $Result | ConvertTo-Json -Compress -Depth 8 | Write-Output
        if ($Result.status -eq "failed") {
            exit 1
        }
        exit 0
    }

    $Stages = @()
    foreach ($Stage in ("preflight", "prematch", "settle")) {
        $Stages += Invoke-TrackerStage $Stage
    }
    $FailedStages = @($Stages | Where-Object { $_.status -eq "failed" })
    $CycleEntry = [pscustomobject]@{
        timestamp = (Get-Date).ToString("o")
        mode = "cycle"
        status = if ($FailedStages.Count -eq 0) { "completed" } else { "completed_with_failures" }
        phases = @($Stages | ForEach-Object {
            [pscustomobject]@{
                mode = $_.mode
                status = $_.status
                recovery_scan = $_.recovery_scan
                error = $_.error
            }
        })
        failed_modes = @($FailedStages | ForEach-Object { $_.mode })
    }
    Write-SchedulerEntry $CycleEntry
    $CycleEntry | ConvertTo-Json -Compress -Depth 8 | Write-Output
} finally {
    if ($Acquired) {
        $Mutex.ReleaseMutex()
    }
    $Mutex.Dispose()
}
