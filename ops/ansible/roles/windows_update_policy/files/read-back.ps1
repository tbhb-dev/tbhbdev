param(
    [string]$Updates,
    [string]$AutomaticUpdates
)

$ErrorActionPreference = 'Stop'
$Ansible.Changed = $false
$mismatches = [System.Collections.Generic.List[string]]::new()
$result = @{ task = $null; policies = @{}; mismatches = $mismatches }
$task = Get-ScheduledTask -TaskName 'Start WSL Ubuntu' -TaskPath '\' -ErrorAction SilentlyContinue
if ($null -eq $task) {
    $mismatches.Add('Missing task: \Start WSL Ubuntu')
} else {
    [xml]$xml = Export-ScheduledTask -TaskName 'Start WSL Ubuntu' -TaskPath '\'
    $result.task = @{ path = $task.TaskPath; name = $task.TaskName; xml = $xml.OuterXml }
    $ns = [System.Xml.XmlNamespaceManager]::new($xml.NameTable)
    $ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')

    # Task Scheduler omits RunLevel=LeastPrivilege, Enabled=true, and StopAtDurationEnd=false from exported XML.
    function Confirm-Text([string]$Path, [string]$Expected, [string]$Default = '') {
        $node = $xml.SelectSingleNode("/t:Task/t:$Path", $ns)
        $actual = if ($null -eq $node) { $Default } else { $node.InnerText }
        if ($actual -cne $Expected) {
            $mismatches.Add("Task ${Path}: expected '$Expected', got '$actual'")
        }
    }

    $sid = (Get-LocalUser -Name tony).SID.Value
    $user = $xml.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:UserId', $ns)
    $actualSid = if ($null -eq $user) { '' } else { $user.InnerText }
    # Windows can export either a SID or an account name for the principal.
    if ($actualSid -and $actualSid -notmatch '^S-\d-') {
        $actualSid = ([System.Security.Principal.NTAccount]::new($actualSid)).Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
    }
    if ($actualSid -ne $sid) {
        $mismatches.Add("Task principal: expected tony ($sid), got '$actualSid'")
    }
    Confirm-Text 'Principals/t:Principal/t:LogonType' 'S4U'
    Confirm-Text 'Principals/t:Principal/t:RunLevel' 'LeastPrivilege' 'LeastPrivilege'
    Confirm-Text 'Actions/t:Exec/t:Command' 'C:\Windows\System32\wsl.exe'
    Confirm-Text 'Actions/t:Exec/t:Arguments' '-d Ubuntu --exec /bin/true'
    Confirm-Text 'Settings/t:MultipleInstancesPolicy' 'IgnoreNew'
    Confirm-Text 'Settings/t:ExecutionTimeLimit' 'PT5M'
    Confirm-Text 'Settings/t:DisallowStartIfOnBatteries' 'false'
    Confirm-Text 'Settings/t:StopIfGoingOnBatteries' 'false'
    Confirm-Text 'Settings/t:Enabled' 'true' 'true'

    foreach ($entry in @(
        @{ Path = 'Actions/*'; Count = 1 },
        @{ Path = 'Triggers/*'; Count = 3 },
        @{ Path = 'Triggers/t:BootTrigger'; Count = 1 },
        @{ Path = 'Triggers/t:TimeTrigger'; Count = 1 },
        @{ Path = 'Triggers/t:EventTrigger'; Count = 1 }
    )) {
        $count = $xml.SelectNodes("/t:Task/t:$($entry.Path)", $ns).Count
        if ($count -ne $entry.Count) {
            $mismatches.Add("Task $($entry.Path): expected $($entry.Count) entries, got $count")
        }
    }
    Confirm-Text 'Triggers/t:BootTrigger/t:Delay' 'PT30S'
    Confirm-Text 'Triggers/t:TimeTrigger/t:StartBoundary' '2026-01-01T00:00:00'
    Confirm-Text 'Triggers/t:TimeTrigger/t:Repetition/t:Interval' 'PT5M'
    Confirm-Text 'Triggers/t:TimeTrigger/t:Repetition/t:Duration' ''
    Confirm-Text 'Triggers/t:TimeTrigger/t:Repetition/t:StopAtDurationEnd' 'false' 'false'
    Confirm-Text 'Triggers/t:EventTrigger/t:Delay' 'PT1M'
    foreach ($type in 'BootTrigger', 'TimeTrigger', 'EventTrigger') {
        Confirm-Text "Triggers/t:$type/t:Enabled" 'true' 'true'
        Confirm-Text "Triggers/t:$type/t:EndBoundary" ''
    }
    $subscription = $xml.SelectSingleNode('/t:Task/t:Triggers/t:EventTrigger/t:Subscription', $ns)
    $expected = @'
<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name='Microsoft-Windows-WindowsUpdateClient'] and EventID=19]]</Select></Query></QueryList>
'@
    if ($null -eq $subscription -or ($subscription.InnerText -replace '\s', '') -cne ($expected -replace '\s', '')) {
        $mismatches.Add('Task event subscription differs from Windows Update event 19 on the System log')
    }
}

$base = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'
foreach ($entry in @(
    @{ Path = $base; Values = ($Updates | ConvertFrom-Json) },
    @{ Path = "$base\AU"; Values = ($AutomaticUpdates | ConvertFrom-Json) }
)) {
    $key = Get-Item -LiteralPath $entry.Path -ErrorAction SilentlyContinue
    $values = @{}
    foreach ($property in $entry.Values.PSObject.Properties) {
        $name = $property.Name
        $value = $null
        $kind = $null
        if ($null -ne $key -and $key.GetValueNames() -contains $name) {
            $value = $key.GetValue($name)
            $kind = $key.GetValueKind($name).ToString()
        }
        $values[$name] = @{ value = $value; type = $kind }
        if ($kind -ne 'DWord' -or $value -ne $property.Value) {
            $mismatches.Add("$($entry.Path)\${name}: expected DWORD $($property.Value), got '$value' ($kind)")
        }
    }
    $result.policies[$entry.Path] = $values
}
$Ansible.Result = $result
