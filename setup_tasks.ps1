<#
.SYNOPSIS
    Registers the AdWriter Windows Scheduled Tasks.

.DESCRIPTION
    Idempotent: safe to re-run. Each task is unregistered first (if present)
    and re-created, so this always leaves the task matching what's defined
    here rather than silently skipping an out-of-date existing registration.

    Tasks run C:\adwriter\adwriter-env\Scripts\python.exe against a script in
    C:\adwriter (or, with -Execute, a wrapper such as run_orchestrator.bat), as
    the currently logged-on user, at "Highest" run level (matches how
    AdWriter-Orchestrator / AdWriter-Browse / etc. were already configured on
    this machine when this file was written).

    The first five tasks below (CTR, ReconWarmup, CarfaxWarmup, Browse,
    Orchestrator) already existed on this machine before this file did; they
    are included here so this script is a complete, accurate record of what
    AdWriter has scheduled, not just the newest addition. AdWriter-Orchestrator
    runs on a specific subset of weekdays rather than daily — if this script
    is ever used to actually recreate that task, verify its DaysOfWeek against
    the live task first (`(Get-ScheduledTask AdWriter-Orchestrator).Triggers`)
    rather than trust the "daily" placeholder used below.

.NOTES
    Run from an elevated PowerShell prompt.
#>

$ErrorActionPreference = "Stop"

$PythonExe = "C:\adwriter\adwriter-env\Scripts\python.exe"
$WorkingDir = "C:\adwriter"
# $env:USERDOMAIN is unreliable here (observed stale as "WORKGROUP" against a
# real computer name of GURYGANGPC, which Register-ScheduledTask then can't
# resolve to a SID) — $env:COMPUTERNAME is the value that actually works.
$User = "$env:COMPUTERNAME\$env:USERNAME"

function Register-AdWriterTask {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [string] $ScriptPath,
        [Parameter(Mandatory)] [datetime] $At,
        [string] $Description = "",
        # Program to run; $ScriptPath is its argument string. Defaults to the
        # venv python, so $ScriptPath is "script.py [args]" for most tasks.
        [string] $Execute = $PythonExe,
        # Stop the task after this many hours, and never start a second copy
        # while one is still running. 0 = Task Scheduler defaults.
        [int] $TimeLimitHours = 0
    )

    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Removing existing task: $Name"
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }

    $action = New-ScheduledTaskAction -Execute $Execute -Argument $ScriptPath -WorkingDirectory $WorkingDir
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
    $extra = @{}
    if ($TimeLimitHours -gt 0) {
        $extra.Settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours $TimeLimitHours) -MultipleInstances IgnoreNew
    }

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Principal $principal -Description $Description @extra | Out-Null
    Write-Host "Registered: $Name (daily at $($At.ToString('HH:mm')))"
}

# --- pre-existing tasks (documented here for completeness) -------------- #
Register-AdWriterTask -Name "AdWriter-CTR" `
    -ScriptPath "C:\adwriter\ctr_database.py --daily-scrape" `
    -At (Get-Date "21:00") `
    -Description "Daily CTR database scrape."

Register-AdWriterTask -Name "AdWriter-ReconWarmup" `
    -ScriptPath "C:\adwriter\recon_warmup.py" `
    -At (Get-Date "22:00") `
    -Description "Daily recon cache warmup."

Register-AdWriterTask -Name "AdWriter-CarfaxWarmup" `
    -ScriptPath "C:\adwriter\carfax_warmup.py" `
    -At (Get-Date "22:30") `
    -Description "Daily Carfax cache warmup."

Register-AdWriterTask -Name "AdWriter-Browse" `
    -ScriptPath "C:\adwriter\scraper.py --source ipacket_browse" `
    -At (Get-Date "23:00") `
    -Description "Daily iPacket inventory browse."

Register-AdWriterTask -Name "AdWriter-Orchestrator" `
    -ScriptPath "C:\adwriter\orchestrator.py" `
    -At (Get-Date "05:00") `
    -Description "Ad build/reprice orchestrator. NOTE: the live task runs on a specific weekday subset, not every day — see this file's header before relying on this registration to recreate it."

# --- new: nightly vision batch reprocessing ------------------------------ #
Register-AdWriterTask -Name "AdWriter-VisionProcess" `
    -ScriptPath "C:\adwriter\vision_processor.py" `
    -At (Get-Date "00:00") `
    -Description "Nightly vision-parse retry for cached Carfax/sticker images the live pipeline couldn't vision-parse inline. Logs to C:\adwriter\vision_process.log."

# --- daily reprice-only orchestrator run ------------------------------------ #
Register-AdWriterTask -Name "AdWriter-Reprice-Daily" `
    -Execute "C:\adwriter\run_orchestrator.bat" `
    -ScriptPath "--reprice-only" `
    -At (Get-Date "07:00") `
    -TimeLimitHours 4 `
    -Description "Daily reprice-only orchestrator run (run_orchestrator.bat --reprice-only): fresh crawl, rewrite price paragraphs for ads whose price changed. 7:00 AM, before the 9:00 AM verifier."

Write-Host "`nAll AdWriter scheduled tasks registered."
Get-ScheduledTask | Where-Object { $_.TaskName -like "AdWriter-*" } |
    Select-Object TaskName, State | Format-Table -AutoSize
