$ErrorActionPreference = "Stop"

$tasks = Get-ScheduledTask -TaskName "ASUSeatWatcher*" -ErrorAction SilentlyContinue
if (-not $tasks) {
    Write-Host "No ASUSeatWatcher* tasks found."
    exit 0
}

Write-Host "Removing $($tasks.Count) ASUSeatWatcher* task(s)..."
$tasks | Unregister-ScheduledTask -Confirm:$false
Write-Host "Done."
