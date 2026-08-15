param(
    [string]$TaskName = "Path-Flow Sales Agent Inbox",
    [int]$IntervalMinutes = 15
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python).Source
$Agent = Join-Path $PSScriptRoot "sales_agent.py"
$LogDir = Join-Path $RepoRoot "logs"
$LogFile = Join-Path $LogDir "sales_agent_inbox.log"

if (-not (Test-Path (Join-Path $RepoRoot ".env"))) {
    throw "Missing .env at $RepoRoot\.env"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Command = "& '$Python' '$Agent' --inbox-only *>> '$LogFile'"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"$Command`"" -WorkingDirectory $RepoRoot

$StartAt = (Get-Date).AddMinutes(1)
$Trigger = New-ScheduledTaskTrigger -Once -At $StartAt -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration ([TimeSpan]::MaxValue)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Checks info@nexccess.com, classifies Path-Flow replies with local Ollama, updates sales_engine.db, and writes a local log." -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName"
Write-Host "Interval: every $IntervalMinutes minutes"
Write-Host "Log: $LogFile"
Write-Host "The task runs only while Windows is available. It does not send outbound campaign mail."
