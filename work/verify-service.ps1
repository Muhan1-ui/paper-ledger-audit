param([int]$Port = 8000)

$ErrorActionPreference = "Stop"
$BaseUrl = "http://127.0.0.1:$Port"
$HealthPath = "/api/health"
$ReviewPath = "/api/reviews"

$health = Invoke-RestMethod "$BaseUrl$HealthPath" -TimeoutSec 10
$review = Invoke-RestMethod "$BaseUrl$ReviewPath" -TimeoutSec 10

if ($health.status -ne "ok" -or $health.database -ne "connected") {
    throw "Service health check did not report a connected database."
}
if ($review.agent.mode -ne "read_only") {
    throw "Review Agent must remain read_only."
}

[pscustomobject]@{
    status = $health.status
    database = $health.database
    review_mode = $review.agent.mode
    prediction_count = $review.scope.prediction_count
    settled_records = $review.calibration.settled_records
} | ConvertTo-Json -Compress
