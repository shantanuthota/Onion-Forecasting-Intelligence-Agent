$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$job = Join-Path $projectRoot "pull_upag_dash.py"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw ".venv\Scripts\python.exe is missing. Run setup_windows.bat first."
}
if (-not (Test-Path -LiteralPath $job -PathType Leaf)) {
    throw "pull_upag_dash.py is missing from $projectRoot."
}

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument ('"{0}" --once --json' -f $job) `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At ([datetime]::Today) `
    -RepetitionInterval (New-TimeSpan -Hours 6)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

$principal = New-ScheduledTaskPrincipal `
    -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName "OnionUPAgDash6h" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Pull UPAg Dash observations into SQLite every six hours." `
    -Force | Out-Null
