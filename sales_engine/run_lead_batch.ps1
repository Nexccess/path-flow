param(
    [string]$Targets = "$PSScriptRoot\lead_batch_targets.json"
)

$ErrorActionPreference = "Stop"

# Force the entire PowerShell <-> Python boundary to UTF-8. Windows
# PowerShell 5.1 otherwise decodes native-process output with the legacy
# console code page, which corrupts Japanese text when piped to Tee-Object.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom
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
