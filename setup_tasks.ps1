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

    The CTR, ReconWarmup, CarfaxWarmup, Browse,
    Orchestrator tasks below already existed on this machine before this file did; they
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
#
# CAUTION — this used to resolve dynamically to whoever ran this script,
# which is exactly what broke it. All "Interactive only" tasks need Task
# Scheduler to see a real interactive/RDP logon (Logon Type 2 or 10) for
# $User at trigger time. On 2026-09-26 four tasks (AdWriter-CTR-Capture,
# AdWriter-CTR-Email, AdWriter-Verifier-Hourly, AdWriter-ReconWarmup) were
# found registered to "adwriter" instead of "19196" and had NEVER fired
# (schtasks Last Result 267011 — task has not run), because "adwriter" is
# only ever reached over SSH (Logon Type 3/8, Network) for Claude Code
# sessions like this one, and structurally never holds an interactive
# desktop session — while "$env:COMPUTERNAME\$env:USERNAME" silently picked
# up "adwriter" whenever this idempotent, safe-to-re-run script happened to
# be run from such a session. "19196" is the account that's actually logged
# in at the console and is what every working AdWriter-* task runs as.
# $User is now hardcoded below (rather than derived from whoever invokes
# this script) specifically so a future re-run from any account — SSH,
# Claude Code, or otherwise — can't silently re-break registration again.
$User = "$env:COMPUTERNAME\19196"

function Register-AdWriterTask {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [string] $ScriptPath = "",
        [Parameter(Mandatory)] [datetime] $At,
        [string] $Description = "",
        # Program to run; $ScriptPath is its argument string (optional). Defaults
        # to the venv python, so $ScriptPath is "script.py [args]" for most tasks.
        [string] $Execute = $PythonExe,
        # Stop the task after this many hours, and never start a second copy
        # while one is still running. 0 = Task Scheduler defaults.
        [int] $TimeLimitHours = 0,
        # If set, the daily trigger repeats every N hours for
        # $RepetitionDurationHours after $At (e.g. -At 08:00
        # -RepetitionIntervalHours 1 -RepetitionDurationHours 12 fires at
        # 08:00, 09:00, ... 20:00 = 13 runs/day).
        [int] $RepetitionIntervalHours = 0,
        [int] $RepetitionDurationHours = 0
    )

    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Removing existing task: $Name"
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }

    $actionArgs = @{ Execute = $Execute; WorkingDirectory = $WorkingDir }
    if ($ScriptPath) { $actionArgs.Argument = $ScriptPath }
    $action = New-ScheduledTaskAction @actionArgs
    if ($RepetitionIntervalHours -gt 0) {
        # New-ScheduledTaskTrigger -Daily does not accept -RepetitionInterval/
        # -RepetitionDuration directly (ambiguous parameter set), and setting
        # $trigger.Repetition.Interval on the object it returns silently fails
        # ("property not found") in this PS version's CIM binding. Building the
        # CalendarTrigger + RepetitionPattern as CIM instances directly is the
        # combination that actually registers a working hourly-repeat schedule
        # (verified live: `schtasks /query /v` shows the Repeat: Every line set).
        $repetitionClass = Get-CimClass -ClassName MSFT_TaskRepetitionPattern -Namespace "Root/Microsoft/Windows/TaskScheduler"
        $repetition = New-CimInstance -CimClass $repetitionClass -ClientOnly -Property @{
            Interval          = "PT$($RepetitionIntervalHours)H"
            Duration          = "PT$($RepetitionDurationHours)H"
            StopAtDurationEnd = $false
        }
        $triggerClass = Get-CimClass -ClassName MSFT_TaskDailyTrigger -Namespace "Root/Microsoft/Windows/TaskScheduler"
        $trigger = New-CimInstance -CimClass $triggerClass -ClientOnly -Property @{
            StartBoundary = $At.ToString("yyyy-MM-ddTHH:mm:ss")
            DaysInterval  = 1
            Enabled       = $true
            Repetition    = $repetition
        }
    } else {
        $trigger = New-ScheduledTaskTrigger -Daily -At $At
    }
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
    $extra = @{}
    if ($TimeLimitHours -gt 0) {
        $extra.Settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours $TimeLimitHours) -MultipleInstances IgnoreNew
    }

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
        -Principal $principal -Description $Description @extra | Out-Null
    if ($RepetitionIntervalHours -gt 0) {
        Write-Host "Registered: $Name (daily at $($At.ToString('HH:mm')), every ${RepetitionIntervalHours}h for ${RepetitionDurationHours}h)"
    } else {
        Write-Host "Registered: $Name (daily at $($At.ToString('HH:mm')))"
    }
}

function Register-AdWriterMultiTriggerTask {
    # Like Register-AdWriterTask, but for a script run on a fixed set of
    # daily clock times (one Daily trigger per time) rather than a single
    # daily time or an hourly repetition window.
    param(
        [Parameter(Mandatory)] [string] $Name,
        [string] $ScriptPath = "",
        [Parameter(Mandatory)] [datetime[]] $AtTimes,
        [string] $Description = "",
        [string] $Execute = $PythonExe
    )

    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Removing existing task: $Name"
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }

    $actionArgs = @{ Execute = $Execute; WorkingDirectory = $WorkingDir }
    if ($ScriptPath) { $actionArgs.Argument = $ScriptPath }
    $action = New-ScheduledTaskAction @actionArgs
    $triggers = $AtTimes | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
    $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $triggers `
        -Principal $principal -Description $Description | Out-Null
    $timesStr = ($AtTimes | ForEach-Object { $_.ToString('HH:mm') }) -join ', '
    Write-Host "Registered: $Name (daily at $timesStr)"
}

# --- all AdWriter tasks, in daily time order ------------------------------ #

Register-AdWriterTask -Name "AdWriter-VisionProcess" `
    -ScriptPath "C:\adwriter\vision_processor.py" `
    -At (Get-Date "00:00") `
    -Description "Nightly vision-parse retry for cached Carfax/sticker images the live pipeline couldn't vision-parse inline. Logs to C:\adwriter\vision_process.log."

Register-AdWriterTask -Name "AdWriter-CTR-Capture" `
    -Execute "C:\adwriter\run_ctr_warmup.bat" `
    -At (Get-Date "05:20") `
    -TimeLimitHours 3 `
    -Description "Daily CTR capture (ctr_warmup.py): Durham retail + Northlake/Charlotte benchmark CTR into ctr_history.db. Was previously only captured 2x/week as a side effect of the full AdWriter-Orchestrator run. 5:20am -- staggered 20 minutes after AdWriter-Orchestrator's 5:00am start (only matters Fri/Sat, when Orchestrator actually runs) so its crawl_inventory() step, the first thing it does and also on scraper.lock, has cleared before this task's own ACVMaxScraper session tries to acquire the lock. Both use the same global scraper.lock (30s wait); a lock loss inside orchestrator.py's crawl_inventory() step is caught by the same handler as its own outer lock and silently exits the WHOLE run with code 0, which is why this is staggered rather than left to contend."

Register-AdWriterTask -Name "AdWriter-Orchestrator" `
    -ScriptPath "C:\adwriter\orchestrator.py" `
    -At (Get-Date "05:00") `
    -Description "Ad build/reprice orchestrator. NOTE: the live task runs on a specific weekday subset, not every day — see this file's header before relying on this registration to recreate it."

Register-AdWriterTask -Name "AdWriter-CTR-Email" `
    -ScriptPath "C:\adwriter\ctr_database.py --daily-scrape" `
    -At (Get-Date "07:00") `
    -Description "Daily CTR summary email -- builds and sends from whatever is already in ctr_history.db for today (does NOT scrape; see AdWriter-CTR-Capture, which runs at 5am and populates today's rows). 7am, 2 hours after capture so it reports on same-day data. Renamed from AdWriter-CTR, which implied it did the scraping."

Register-AdWriterTask -Name "AdWriter-Reprice-Daily" `
    -Execute "C:\adwriter\run_orchestrator.bat" `
    -ScriptPath "--reprice-only" `
    -At (Get-Date "07:00") `
    -TimeLimitHours 4 `
    -Description "Daily reprice-only orchestrator run (run_orchestrator.bat --reprice-only): fresh crawl, rewrite price paragraphs for ads whose price changed. 7:00 AM, before the 9:00 AM verifier. Shares this exact minute with AdWriter-CTR-Email, which is safe -- that task never touches a scraper or scraper.lock."

Register-AdWriterTask -Name "AdWriter-Verifier-Hourly" `
    -Execute "C:\adwriter\run_verifier.bat" `
    -At (Get-Date "08:00") `
    -RepetitionIntervalHours 1 -RepetitionDurationHours 12 `
    -TimeLimitHours 4 `
    -Description "Standalone verifier, hourly 8am-8pm (13 runs/day): inventory crawl, then hendrickcars.com ad verification. run_verifier.bat itself passes --all --no-email to verifier.py. Replaces the old AM (9am) / PM (8pm) split tasks."

Register-AdWriterMultiTriggerTask -Name "AdWriter-ReconWarmup" `
    -ScriptPath "C:\adwriter\recon_warmup.py" `
    -AtTimes @((Get-Date "08:15"), (Get-Date "13:15"), (Get-Date "17:15"), (Get-Date "20:15")) `
    -Description "Recon cache warmup, 4x daily at :15 past the hour (8:15am/1:15pm/5:15pm/8:15pm). Offset from the on-the-hour verifier runs so the two don't collide on scraper.lock, which only waits 30s before giving up. Replaces the old single 10pm run."

Register-AdWriterTask -Name "AdWriter-CarfaxWarmup" `
    -ScriptPath "C:\adwriter\carfax_warmup.py" `
    -At (Get-Date "22:30") `
    -Description "Daily Carfax cache warmup."

Register-AdWriterTask -Name "AdWriter-Browse" `
    -ScriptPath "C:\adwriter\scraper.py --source ipacket_browse" `
    -At (Get-Date "23:00") `
    -Description "Daily iPacket inventory browse."

Write-Host "`nAll AdWriter scheduled tasks registered."
Get-ScheduledTask | Where-Object { $_.TaskName -like "AdWriter-*" } |
    Select-Object TaskName, State | Format-Table -AutoSize
