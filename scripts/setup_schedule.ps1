$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python     = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$Script     = Join-Path $ProjectDir "src\watcher.py"
$ConfigPath = Join-Path $ProjectDir "config.json"

if (-not (Test-Path $Python)) {
    Write-Error "Python venv not found at $Python. Create it first: python -m venv .venv"
    exit 1
}
if (-not (Test-Path $Script)) {
    Write-Error "Watcher script not found at $Script"
    exit 1
}
if (-not (Test-Path $ConfigPath)) {
    Write-Error "config.json not found at $ConfigPath"
    exit 1
}

$Config = Get-Content $ConfigPath -Raw | ConvertFrom-Json

# Remove any existing tasks first (covers both old per-time tasks and the
# single repeating task this version creates).
$existing = Get-ScheduledTask -TaskName "ASUSeatWatcher*" -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing $($existing.Count) existing ASUSeatWatcher* task(s)..."
    $existing | Unregister-ScheduledTask -Confirm:$false
}

$intervalMinutes = [int]$Config.schedule_interval_minutes
if (-not $intervalMinutes -or $intervalMinutes -le 0) {
    Write-Error "config.json must define schedule_interval_minutes (e.g. 15)"
    exit 1
}
$startTimeStr = $Config.schedule_start_time
if (-not $startTimeStr) { $startTimeStr = "00:00" }

# Start at today's $startTimeStr; if that's in the future, Task Scheduler
# will simply wait. RepetitionDuration of [TimeSpan]::MaxValue means
# "forever" — works on PowerShell 5.1+.
$today = Get-Date
$startTime = Get-Date -Year $today.Year -Month $today.Month -Day $today.Day `
    -Hour ([int]$startTimeStr.Split(':')[0]) `
    -Minute ([int]$startTimeStr.Split(':')[1]) -Second 0

$trigger = New-ScheduledTaskTrigger `
    -Once -At $startTime `
    -RepetitionInterval (New-TimeSpan -Minutes $intervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`"" -WorkingDirectory $ProjectDir

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

try {
    Register-ScheduledTask `
        -TaskName "ASUSeatWatcher" `
        -Trigger $trigger `
        -Action $action `
        -Settings $settings `
        -Description "ASU MAT 243 seat poll, every $intervalMinutes minutes starting $startTimeStr" `
        -ErrorAction Stop | Out-Null
} catch {
    Write-Error "Failed to register ASUSeatWatcher task: $_"
    exit 1
}

$runsPerDay = [math]::Floor(1440 / $intervalMinutes)
Write-Host ""
Write-Host "Registered ASUSeatWatcher: every $intervalMinutes minutes (~$runsPerDay runs/day)."
Write-Host "Start time: $startTime"
Write-Host ""
Write-Host "To inspect:"
Write-Host "  Get-ScheduledTask -TaskName 'ASUSeatWatcher' | Get-ScheduledTaskInfo"
Write-Host "To remove:"
Write-Host "  .\scripts\unregister_schedule.ps1"
