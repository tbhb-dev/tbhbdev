[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'
$Ansible.Changed = $false
$task = Get-ScheduledTask -TaskName 'Start WSL Ubuntu' -TaskPath '\' -ErrorAction SilentlyContinue
if ($null -eq $task) {
    throw 'Missing task: \Start WSL Ubuntu; create it outside this role.'
}

# Windows may return the local account name, a qualified name, or its SID.
$sid = (Get-LocalUser -Name tony).SID.Value
$actualSid = $task.Principal.UserId
if ($actualSid -and $actualSid -notmatch '^S-\d-') {
    $actualSid = ([System.Security.Principal.NTAccount]::new($actualSid)).Translate(
        [System.Security.Principal.SecurityIdentifier]
    ).Value
}
if ($actualSid -ne $sid -or $task.Principal.LogonType -ne 'S4U' -or $task.Principal.RunLevel -ne 'Limited') {
    throw "Task principal must remain tony/S4U/Limited; found '$($task.Principal.UserId)/$($task.Principal.LogonType)/$($task.Principal.RunLevel)'."
}
$actions = @($task.Actions)
if ($actions.Count -ne 1 -or $actions[0].CimClass.CimClassName -ne 'MSFT_TaskExecAction' -or
    $actions[0].Execute -cne 'C:\Windows\System32\wsl.exe' -or
    $actions[0].Arguments -cne '-d Ubuntu --exec /bin/true') {
    throw 'Task action must remain C:\Windows\System32\wsl.exe -d Ubuntu --exec /bin/true.'
}

$boot = New-ScheduledTaskTrigger -AtStartup
$boot.Delay = 'PT30S'
$boot.Enabled = $true
$startBoundary = '2026-01-01T00:00:00'
$time = New-ScheduledTaskTrigger -Once -At ([datetime]$startBoundary) -RepetitionInterval (New-TimeSpan -Minutes 5)
# Keep a fixed past boundary and no repetition duration or end boundary.
$time.StartBoundary = $startBoundary
$time.Repetition.Duration = ''
$time.Repetition.StopAtDurationEnd = $false
$time.Enabled = $true
$event = New-CimInstance -CimClass (Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskEventTrigger) -ClientOnly -Property @{
    Subscription = @'
<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[ Provider[@Name='Microsoft-Windows-WindowsUpdateClient'] and EventID=19]]</Select></Query></QueryList>
'@
    Delay = 'PT1M'
    Enabled = $true
}
$wanted = @($boot, $time, $event)

function Get-TriggerSignature($Trigger) {
    $fields = [ordered]@{
        Type = $Trigger.CimClass.CimClassName
        Enabled = [bool]$Trigger.Enabled
        EndBoundary = [string]$Trigger.EndBoundary
    }
    switch ($fields.Type) {
        'MSFT_TaskBootTrigger' { $fields.Delay = [string]$Trigger.Delay }
        'MSFT_TaskTimeTrigger' {
            $fields.StartBoundary = [string]$Trigger.StartBoundary
            $fields.Interval = [string]$Trigger.Repetition.Interval
            $fields.Duration = [string]$Trigger.Repetition.Duration
            $fields.StopAtDurationEnd = [bool]$Trigger.Repetition.StopAtDurationEnd
        }
        'MSFT_TaskEventTrigger' {
            $fields.Subscription = $Trigger.Subscription -replace '\s', ''
            $fields.Delay = [string]$Trigger.Delay
        }
    }
    $fields | ConvertTo-Json -Compress
}

# Sorting compares a set of triggers, including duplicates, without relying on order.
$existingSignatures = @($task.Triggers | ForEach-Object { Get-TriggerSignature $_ } | Sort-Object)
$wantedSignatures = @($wanted | ForEach-Object { Get-TriggerSignature $_ } | Sort-Object)
$triggersDiffer = ($existingSignatures -join "`n") -cne ($wantedSignatures -join "`n")

# Copy just these settings into the existing object so other settings survive.
$wantedSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$settings = $task.Settings
$settingNames = @('MultipleInstances', 'ExecutionTimeLimit', 'DisallowStartIfOnBatteries', 'StopIfGoingOnBatteries')
$settingsDiffer = @($settingNames | Where-Object { $settings.$_ -ne $wantedSettings.$_ }).Count -gt 0
$Ansible.Changed = $triggersDiffer -or $settingsDiffer
$Ansible.Result = @{ triggers_changed = $triggersDiffer; settings_changed = $settingsDiffer }
if (-not $Ansible.CheckMode) {
    # Supplying only triggers or settings preserves the S4U principal and action.
    if ($triggersDiffer) {
        Set-ScheduledTask -TaskName 'Start WSL Ubuntu' -TaskPath '\' -Trigger $wanted | Out-Null
    }
    if ($settingsDiffer) {
        foreach ($name in $settingNames) { $settings.$name = $wantedSettings.$name }
        Set-ScheduledTask -TaskName 'Start WSL Ubuntu' -TaskPath '\' -Settings $settings | Out-Null
    }
}
