param(
    [string]$Targets = "$PSScriptRoot\lead_batch_targets.json"
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Set-Location $PSScriptRoot

$logDir = Join-Path $PSScriptRoot "logs\lead_batch"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logPath = Join-Path $logDir "run-$stamp.log"

"[$(Get-Date -Format s)] Nexccess Lead Batch start" | Tee-Object -FilePath $logPath

try {
    & python ".\lead_batch.py" --targets $Targets 2>&1 |
        Tee-Object -FilePath $logPath -Append
    $exitCode = $LASTEXITCODE
} catch {
    $_ | Out-String | Tee-Object -FilePath $logPath -Append
    $exitCode = 1
}

"[$(Get-Date -Format s)] Nexccess Lead Batch exit=$exitCode" |
    Tee-Object -FilePath $logPath -Append

exit $exitCode
