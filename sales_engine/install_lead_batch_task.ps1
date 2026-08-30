param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$At,

    [string]$TaskName = "Nexccess-Lead-Batch"
)

$ErrorActionPreference = "Stop"

$runner = Join-Path $PSScriptRoot "run_lead_batch.ps1"
if (-not (Test-Path $runner)) {
    throw "Runner not found: $runner"
}

$parts = $At.Split(':')
$hour = [int]$parts[0]
$minute = [int]$parts[1]
$triggerTime = (Get-Date).Date.AddHours($hour).AddMinutes($minute)

$actionArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArgs -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $triggerTime
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Nexccess Lead Discovery -> Screening -> Sales Queue nightly batch" `
    -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Daily at: $At"
Write-Host "Runner: $runner"
Write-Host "Reports: $PSScriptRoot\logs\lead_batch\latest.json"
