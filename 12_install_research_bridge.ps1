param(
    [ValidateSet("Install", "Uninstall")]
    [string]$Mode = "Install"
)

$ErrorActionPreference = "Stop"

$TaskName      = "InstagramResearchBridge"
$ExpectedVer   = "0.6.0"
$App           = $PSScriptRoot
$Root          = (Resolve-Path (Join-Path $App "..")).Path
$StateDir      = Join-Path $Root "state"
$ControlDir    = Join-Path $Root "control"
$Bridge        = Join-Path $App "research_request_bridge.py"
$RecoveryTaskName = "InstagramResearchBridgeRecovery"
$RecoveryScript = Join-Path $App "BACKLOG_051_recovery_check.ps1"
$RecoveryConfig = Join-Path $App "backlog_051_recovery_config.json"
$RecoveryTaskSpec = Join-Path $App "backlog_051_recovery_task_definition.json"
$PrimaryTaskSpec = Join-Path $App "backlog_051_primary_task_definition.json"

$Status        = Join-Path $StateDir "research_bridge_status.json"
$BridgeState   = Join-Path $StateDir "research_bridge_state.json"
$Request       = Join-Path $ControlDir "research_bridge_request.json"
$InstallLog    = Join-Path $Root "logs\research_bridge_install.log"
$LocalRecoveryDir = Join-Path $env:LOCALAPPDATA "InstagramResearch\recovery"
$DesiredStatePath = Join-Path $LocalRecoveryDir "desired_state.json"
$RecoveryStatePath = Join-Path $LocalRecoveryDir "recovery_state.json"
$BridgeStateUpgradeBackup = Join-Path $LocalRecoveryDir "research_bridge_state.pre_v0.6.0-upgrade.json"
$BridgeStateUpgradeTransaction = Join-Path $LocalRecoveryDir "research_bridge_state.pre_v0.6.0-upgrade.transaction.json"
$RecoveryLockPath = Join-Path $env:LOCALAPPDATA "InstagramResearch\recovery.guard.lock"

$Py       = Join-Path $env:LOCALAPPDATA "InstagramResearch\venv\Scripts\python.exe"
$Pyw      = Join-Path $env:LOCALAPPDATA "InstagramResearch\venv\Scripts\pythonw.exe"
$TaskKill = Join-Path $env:SystemRoot "System32\taskkill.exe"
$PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$TaskArguments = '"' + $Bridge + '" --root "' + $Root + '"'
$RecoveryArguments = '-c "import os,subprocess,sys;sys.exit(subprocess.run([os.path.join(os.environ[''SystemRoot''],''System32'',''WindowsPowerShell'',''v1.0'',''powershell.exe''),''-NoProfile'',''-NonInteractive'',''-WindowStyle'',''Hidden'',''-ExecutionPolicy'',''Bypass'',''-File'',os.path.join(os.getcwd(),''BACKLOG_051_recovery_check.ps1'')],creationflags=subprocess.CREATE_NO_WINDOW,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode)"'

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$InstallStarted = [DateTimeOffset]::UtcNow

New-Item -ItemType Directory -Force -Path (Split-Path $InstallLog) | Out-Null
New-Item -ItemType Directory -Force -Path $LocalRecoveryDir | Out-Null

function Log([string]$Message) {
    $line = ([DateTimeOffset]::UtcNow.ToString("o") + " " + $Message)
    Write-Host $line
    Add-Content -LiteralPath $InstallLog -Value $line -Encoding UTF8
}

function Wait-For {
    param(
        [scriptblock]$Condition,
        [int]$Seconds,
        [string]$Failure
    )

    $deadline = (Get-Date).AddSeconds($Seconds)

    while ((Get-Date) -lt $deadline) {
        try {
            if (& $Condition) {
                return
            }
        }
        catch {
        }

        Start-Sleep -Milliseconds 500
    }

    throw $Failure
}

function Get-ExpectedBridgeTaskActionIdentity {
    $separator = [char]0
    $expectedCommandLine = '"' + $Pyw + '" ' + $TaskArguments
    return [pscustomobject]@{
        Execute = $Pyw
        Arguments = $TaskArguments
        WorkingDirectory = $App
        ExpectedCommandLine = $expectedCommandLine
        IdentityKey = ($Pyw.ToLowerInvariant() + $separator + $TaskArguments + $separator + $App.ToLowerInvariant())
    }
}

function Get-BridgeTaskActionIdentity {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $actions = @($task.Actions)
    if ($actions.Count -ne 1) { throw ("Primary task must have exactly one action; found " + $actions.Count) }

    $expected = Get-ExpectedBridgeTaskActionIdentity
    $action = $actions[0]
    if (
        [string]$action.Execute -ine [string]$expected.Execute -or
        [string]$action.Arguments -cne [string]$expected.Arguments -or
        [string]$action.WorkingDirectory -ine [string]$expected.WorkingDirectory
    ) { throw "Primary task action identity drifted from the reviewed bridge action." }
    return $expected
}


function Get-PrimaryTaskRunningInstancePids {
    $service=$null
    try {
        $service=New-Object -ComObject "Schedule.Service"
        $service.Connect()
        $task=$service.GetFolder("\").GetTask("$TaskName")
        $instances=@($task.GetInstances(0))
        $pids=@()
        foreach($instance in $instances){
            $enginePid=[int]$instance.EnginePID
            if($enginePid -le 0){throw "Task Scheduler returned an invalid EnginePID."}
            if($pids -contains $enginePid){throw "Task Scheduler returned a duplicate EnginePID."}
            $pids += $enginePid
        }
        return $pids
    }
    catch {
        throw ("Primary task running-instance PID attestation failed: " + $_.Exception.Message)
    }
}

function Assert-PrimaryTaskRootAttestation {
    param($Snapshot)
    if($null -eq $Snapshot -or !$Snapshot.IsValid -or $null -eq $Snapshot.TaskRoot){throw "Cannot attest an invalid bridge snapshot."}
    $instancePids=@(Get-PrimaryTaskRunningInstancePids)
    if($instancePids.Count -ne 1){throw ("Primary task must have exactly one running Scheduler instance while a bridge root is live; found="+$instancePids.Count)}
    if([int]$instancePids[0] -ne [int]$Snapshot.TaskRoot.ProcessId){throw ("Bridge root PID is not the running Scheduled Task EnginePID; root="+[int]$Snapshot.TaskRoot.ProcessId+" engine="+[int]$instancePids[0])}
    return $true
}

function Get-BridgeProcesses {
    param($TaskAction)

    if ($null -eq $TaskAction) { $TaskAction = Get-BridgeTaskActionIdentity }
    $all = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $roots = @(
        $all | Where-Object {
            $_.CommandLine -and $_.ExecutablePath -and
            [string]$_.ExecutablePath -ieq [string]$TaskAction.Execute -and
            [string]$_.CommandLine -ieq [string]$TaskAction.ExpectedCommandLine
        }
    )
    if ($roots.Count -eq 0) { return @() }

    $selected = @{}
    $queue = New-Object System.Collections.Generic.Queue[object]
    foreach ($root in $roots) {
        $pidValue = [int]$root.ProcessId
        if (!$selected.ContainsKey($pidValue)) {
            $selected[$pidValue] = $root
            $queue.Enqueue($root)
        }
    }

    while ($queue.Count -gt 0) {
        $parent = $queue.Dequeue()
        foreach ($child in @($all | Where-Object { [int]$_.ParentProcessId -eq [int]$parent.ProcessId })) {
            $childPid = [int]$child.ProcessId
            if (!$selected.ContainsKey($childPid)) {
                $selected[$childPid] = $child
                $queue.Enqueue($child)
            }
        }
    }
    return @($selected.Values)
}

function Convert-ToBridgeProcessIdentity {
    param($Process)
    if ($null -eq $Process -or $null -eq $Process.CreationDate -or [string]::IsNullOrWhiteSpace([string]$Process.ExecutablePath) -or [string]::IsNullOrWhiteSpace([string]$Process.CommandLine)) { return $null }
    try { $creationTicks = ([DateTimeOffset]$Process.CreationDate).ToUniversalTime().Ticks } catch { return $null }
    $processId = [int]$Process.ProcessId
    $parentProcessId = [int]$Process.ParentProcessId
    $executable = [string]$Process.ExecutablePath
    $commandLine = [string]$Process.CommandLine
    $separator = [char]0
    $identityKey = ($processId.ToString() + $separator + $parentProcessId.ToString() + $separator + ([long]$creationTicks).ToString() + $separator + $executable.ToLowerInvariant() + $separator + $commandLine)
    return [pscustomobject]@{ ProcessId=$processId; ParentProcessId=$parentProcessId; CreationUtcTicks=[long]$creationTicks; ExecutablePath=$executable; CommandLine=$commandLine; IdentityKey=$identityKey }
}

function New-BridgeProcessSnapshot {
    param([object[]]$Processes, $TaskAction)
    $failures = New-Object System.Collections.Generic.List[string]
    $nodes = @()
    $byPid = @{}
    $rawCount = @($Processes).Count

    foreach ($process in @($Processes)) {
        $node = Convert-ToBridgeProcessIdentity $process
        if ($null -eq $node) { $failures.Add("missing_process_identity"); continue }
        if ($byPid.ContainsKey([int]$node.ProcessId)) { $failures.Add("duplicate_pid=" + $node.ProcessId); continue }
        $byPid[[int]$node.ProcessId] = $node
        $nodes += $node
    }
    if ($nodes.Count -eq 0) { $failures.Add("no_matching_processes") }
    if ($nodes.Count -ne $rawCount) { $failures.Add("incomplete_descendant_identity_set") }

    $roots = @($nodes | Where-Object { !$byPid.ContainsKey([int]$_.ParentProcessId) })
    if ($roots.Count -ne 1) { $failures.Add("root_count=" + $roots.Count) }
    $taskRoot = $null
    if ($roots.Count -eq 1) {
        $root = $roots[0]
        if ($null -eq $TaskAction -or [string]$root.ExecutablePath -ine [string]$TaskAction.Execute) { $failures.Add("root_not_current_task_action") }
        elseif (![string]::Equals([string]$root.CommandLine,[string]$TaskAction.ExpectedCommandLine,[System.StringComparison]::OrdinalIgnoreCase)) { $failures.Add("root_commandline_not_current_task_action") }
        else { $taskRoot = $root }
    }

    if ($null -ne $taskRoot) {
        $visited = @{}
        $queue = New-Object System.Collections.Generic.Queue[object]
        $visited[[int]$taskRoot.ProcessId] = $true
        $queue.Enqueue($taskRoot)
        while ($queue.Count -gt 0) {
            $parent = $queue.Dequeue()
            foreach ($child in @($nodes | Where-Object { [int]$_.ParentProcessId -eq [int]$parent.ProcessId })) {
                if ($visited.ContainsKey([int]$child.ProcessId)) { $failures.Add("cycle_detected=" + $child.ProcessId); continue }
                if ([long]$parent.CreationUtcTicks -ge [long]$child.CreationUtcTicks) { $failures.Add("stale_parent_edge=" + $parent.ProcessId + "->" + $child.ProcessId); continue }
                $visited[[int]$child.ProcessId] = $true
                $queue.Enqueue($child)
            }
        }
        if ($visited.Count -ne $nodes.Count) { $failures.Add("disconnected_or_cyclic_component") }
    }

    $parentPids = @{}
    foreach ($node in $nodes) { $parentPids[[int]$node.ParentProcessId] = $true }
    $leaves = @($nodes | Where-Object { !$parentPids.ContainsKey([int]$_.ProcessId) })
    $uniqueFailures = @($failures | Select-Object -Unique)
    $orderedProcessKeys = @($nodes | Sort-Object ProcessId | ForEach-Object { $_.IdentityKey })
    $snapshotIdentity = if ($TaskAction) { [string]$TaskAction.IdentityKey + "`n" + ($orderedProcessKeys -join "`n") } else { ($orderedProcessKeys -join "`n") }
    return [pscustomobject]@{ IsValid=($uniqueFailures.Count -eq 0); FailureReasons=$uniqueFailures; Processes=$nodes; Roots=$roots; Leaves=$leaves; TaskRoot=$taskRoot; TaskAction=$TaskAction; SnapshotIdentity=$snapshotIdentity }
}

function Get-BridgeProcessSnapshot {
    $taskAction = Get-BridgeTaskActionIdentity
    return (New-BridgeProcessSnapshot -Processes @(Get-BridgeProcesses -TaskAction $taskAction) -TaskAction $taskAction)
}

function Assert-BridgeSnapshotStable {
    param($Expected, $Actual, [string]$Context)
    if ($null -eq $Expected -or !$Expected.IsValid) { throw ($Context + ": expected snapshot is invalid") }
    if ($null -eq $Actual -or !$Actual.IsValid) { $reason=if($Actual){(@($Actual.FailureReasons)-join ",")}else{"missing"}; throw ($Context + ": current snapshot is invalid: " + $reason) }
    if (![string]::Equals([string]$Expected.SnapshotIdentity,[string]$Actual.SnapshotIdentity,[System.StringComparison]::Ordinal)) { throw ($Context + ": process/task identity drifted") }
}

function Get-BridgeProcessById {
    param([int]$ProcessId)
    $items = @(Get-CimInstance Win32_Process -Filter ("ProcessId = " + $ProcessId) -ErrorAction Stop)
    if ($items.Count -ne 1) { return $null }
    return (Convert-ToBridgeProcessIdentity $items[0])
}

function Initialize-BridgeSuspendApi {
    if ($null -ne ("Backlog051.NativeProcessControl" -as [type])) { return }
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace Backlog051 {
    public static class NativeProcessControl {
        private const uint PROCESS_SUSPEND_RESUME = 0x0800;
        private const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
        private const uint SYNCHRONIZE = 0x00100000;

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr OpenProcess(uint dwDesiredAccess, bool bInheritHandle, int dwProcessId);

        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr hObject);

        [DllImport("ntdll.dll")]
        public static extern int NtSuspendProcess(IntPtr processHandle);

        [DllImport("ntdll.dll")]
        public static extern int NtResumeProcess(IntPtr processHandle);

        public static IntPtr OpenForSuspend(int processId) {
            return OpenProcess(PROCESS_SUSPEND_RESUME | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, false, processId);
        }
    }
}
"@ -Language CSharp -ErrorAction Stop
}

function Open-BridgeSuspendGuard {
    param($Identity)
    if ($null -eq $Identity) { throw "Cannot guard missing bridge process identity." }
    Initialize-BridgeSuspendApi
    $handle=[Backlog051.NativeProcessControl]::OpenForSuspend([int]$Identity.ProcessId)
    if ($handle -eq [IntPtr]::Zero) {
        $win32=[Runtime.InteropServices.Marshal]::GetLastWin32Error()
        throw ("Cannot open bridge process for suspend/resume; PID="+[int]$Identity.ProcessId+" win32="+$win32)
    }
    try {
        $now=Get-BridgeProcessById -ProcessId ([int]$Identity.ProcessId)
        if($null -eq $now -or ![string]::Equals([string]$Identity.IdentityKey,[string]$now.IdentityKey,[System.StringComparison]::Ordinal)){
            throw ("Bridge process identity drifted while opening suspend guard; PID="+[int]$Identity.ProcessId)
        }
        return [pscustomobject]@{Identity=$Identity;NativeHandle=$handle;Suspended=$false}
    } catch {
        [Backlog051.NativeProcessControl]::CloseHandle($handle) | Out-Null
        throw
    }
}

function Suspend-BridgeGuard {
    param($Guard)
    if($null -eq $Guard -or $Guard.NativeHandle -eq [IntPtr]::Zero){throw "Invalid bridge suspend guard."}
    if([bool]$Guard.Suspended){return}
    $status=[Backlog051.NativeProcessControl]::NtSuspendProcess($Guard.NativeHandle)
    if($status -ne 0){throw("NtSuspendProcess failed for PID "+[int]$Guard.Identity.ProcessId+" NTSTATUS=0x"+("{0:X8}" -f ([uint32]$status)))}
    $Guard.Suspended=$true
}

function Resume-BridgeGuard {
    param($Guard)
    if($null -eq $Guard -or ![bool]$Guard.Suspended){return}
    $identityNow=Get-BridgeProcessById -ProcessId ([int]$Guard.Identity.ProcessId)
    if($null -eq $identityNow){
        $Guard.Suspended=$false
        return
    }
    if(![string]::Equals([string]$Guard.Identity.IdentityKey,[string]$identityNow.IdentityKey,[System.StringComparison]::Ordinal)){
        throw("Refusing to resume PID-reused/drifted process "+[int]$Guard.Identity.ProcessId)
    }
    $status=[Backlog051.NativeProcessControl]::NtResumeProcess($Guard.NativeHandle)
    if($status -ne 0){throw("NtResumeProcess failed for PID "+[int]$Guard.Identity.ProcessId+" NTSTATUS=0x"+("{0:X8}" -f ([uint32]$status)))}
    $Guard.Suspended=$false
}

function Close-BridgeGuard {
    param($Guard)
    if($null -eq $Guard -or $Guard.NativeHandle -eq [IntPtr]::Zero){return}
    [Backlog051.NativeProcessControl]::CloseHandle($Guard.NativeHandle) | Out-Null
    $Guard.NativeHandle=[IntPtr]::Zero
}

function Assert-SnapshotContainsExpected {
    param($Expected, $Actual, [string]$Context)
    if($null -eq $Expected -or !$Expected.IsValid){throw($Context+": expected snapshot invalid")}
    if($null -eq $Actual -or !$Actual.IsValid){$reason=if($Actual){(@($Actual.FailureReasons)-join ",")}else{"missing"};throw($Context+": current snapshot invalid: "+$reason)}
    if($null -eq $Expected.TaskRoot -or $null -eq $Actual.TaskRoot -or ![string]::Equals([string]$Expected.TaskRoot.IdentityKey,[string]$Actual.TaskRoot.IdentityKey,[System.StringComparison]::Ordinal)){
        throw($Context+": task-root identity drifted")
    }
    $actualByPid=@{}
    foreach($node in @($Actual.Processes)){$actualByPid[[int]$node.ProcessId]=$node}
    foreach($node in @($Expected.Processes)){
        if(!$actualByPid.ContainsKey([int]$node.ProcessId)){throw($Context+": previously validated descendant disappeared; PID="+[int]$node.ProcessId)}
        if(![string]::Equals([string]$node.IdentityKey,[string]$actualByPid[[int]$node.ProcessId].IdentityKey,[System.StringComparison]::Ordinal)){throw($Context+": descendant identity drifted; PID="+[int]$node.ProcessId)}
    }
}

function Enter-BridgeTreeDestructiveQuiescence {
    param($ExpectedSnapshot, [int]$MaxRounds=8, [int]$MaxProcesses=64)
    if($null -eq $ExpectedSnapshot -or !$ExpectedSnapshot.IsValid){throw "Cannot quiesce invalid bridge process snapshot."}
    if($MaxRounds -lt 2 -or $MaxProcesses -lt 1){throw "Invalid destructive-quiescence bounds."}
    $guards=@{}
    $current=$null
    $stableRounds=0
    try {
        $current=Get-BridgeProcessSnapshot
        Assert-SnapshotContainsExpected -Expected $ExpectedSnapshot -Actual $current -Context "pre-kill quiescence entry"
        for($round=1;$round -le $MaxRounds;$round++){
            if(@($current.Processes).Count -gt $MaxProcesses){throw("Bridge tree exceeds bounded destructive-quiescence process cap="+$MaxProcesses)}
            foreach($existing in @($guards.Values)){
                $now=Get-BridgeProcessById -ProcessId ([int]$existing.Identity.ProcessId)
                if($null -eq $now -or ![string]::Equals([string]$existing.Identity.IdentityKey,[string]$now.IdentityKey,[System.StringComparison]::Ordinal)){
                    throw("Guarded bridge descendant disappeared/drifted during quiescence; PID="+[int]$existing.Identity.ProcessId)
                }
                if(![bool]$existing.Suspended){throw("Guarded bridge descendant unexpectedly not suspended; PID="+[int]$existing.Identity.ProcessId)}
            }

            $newNodes=@($current.Processes | Where-Object { !$guards.ContainsKey([int]$_.ProcessId) } | Sort-Object @{Expression={if([int]$_.ProcessId -eq [int]$current.TaskRoot.ProcessId){0}else{1}}}, CreationUtcTicks, ProcessId)
            foreach($node in $newNodes){
                if($guards.Count -ge $MaxProcesses){throw("Bridge tree exceeds bounded destructive-quiescence process cap="+$MaxProcesses)}
                $guard=Open-BridgeSuspendGuard -Identity $node
                try {
                    Suspend-BridgeGuard -Guard $guard
                    $identityNow=Get-BridgeProcessById -ProcessId ([int]$node.ProcessId)
                    if($null -eq $identityNow -or ![string]::Equals([string]$node.IdentityKey,[string]$identityNow.IdentityKey,[System.StringComparison]::Ordinal)){
                        throw("Bridge descendant identity drifted at suspend boundary; PID="+[int]$node.ProcessId)
                    }
                    $guards[[int]$node.ProcessId]=$guard
                } catch {
                    try { if([bool]$guard.Suspended){Resume-BridgeGuard -Guard $guard} } catch {}
                    Close-BridgeGuard -Guard $guard
                    throw
                }
            }

            $next=Get-BridgeProcessSnapshot
            Assert-SnapshotContainsExpected -Expected $ExpectedSnapshot -Actual $next -Context "quiesced complete-lineage resnapshot"
            $nextByPid=@{}
            foreach($node in @($next.Processes)){$nextByPid[[int]$node.ProcessId]=$node}
            foreach($guard in @($guards.Values)){
                if(!$nextByPid.ContainsKey([int]$guard.Identity.ProcessId)){throw("Suspended descendant missing from complete-lineage resnapshot; PID="+[int]$guard.Identity.ProcessId)}
                if(![string]::Equals([string]$guard.Identity.IdentityKey,[string]$nextByPid[[int]$guard.Identity.ProcessId].IdentityKey,[System.StringComparison]::Ordinal)){throw("Suspended descendant identity drifted in complete-lineage resnapshot; PID="+[int]$guard.Identity.ProcessId)}
            }

            $unguarded=@($next.Processes | Where-Object { !$guards.ContainsKey([int]$_.ProcessId) })
            if($unguarded.Count -eq 0){
                $stableRounds++
                if($stableRounds -ge 2){
                    return [pscustomobject]@{Snapshot=$next;Guards=@($guards.Values);Rounds=$round}
                }
                Start-Sleep -Milliseconds 50
            } else {
                $stableRounds=0
            }
            $current=$next
        }
        throw("Bridge tree did not reach a fully suspended stable membership set within "+$MaxRounds+" rounds.")
    } catch {
        $primaryError=$_.Exception.Message
        $resumeError=$null
        foreach($guard in @($guards.Values | Sort-Object {$_.Identity.CreationUtcTicks} -Descending)){
            try { Resume-BridgeGuard -Guard $guard } catch { if($null -eq $resumeError){$resumeError=$_.Exception.Message} }
            try { Close-BridgeGuard -Guard $guard } catch {}
        }
        if($null -ne $resumeError){throw($primaryError+"; additionally failed to restore quiesced process: "+$resumeError)}
        throw
    }
}

function Release-BridgeTreeDestructiveQuiescence {
    param($Quiescence, [bool]$Resume)
    if($null -eq $Quiescence){return}
    $resumeError=$null
    foreach($guard in @($Quiescence.Guards | Sort-Object {$_.Identity.CreationUtcTicks} -Descending)){
        if($Resume){
            try { Resume-BridgeGuard -Guard $guard } catch { if($null -eq $resumeError){$resumeError=$_.Exception.Message} }
        }
        try { Close-BridgeGuard -Guard $guard } catch {}
    }
    if($null -ne $resumeError){throw("Failed to restore bridge tree after aborted destructive boundary: "+$resumeError)}
}

function Invoke-TaskKillTree { param([int]$ProcessId); & $TaskKill /PID $ProcessId /T /F | Out-Null; if($LASTEXITCODE -ne 0){throw("taskkill failed for bridge task-root PID "+$ProcessId+"; exit="+$LASTEXITCODE)} }

function Invoke-ValidatedBridgeTreeKill {
    param($ExpectedSnapshot, $RecoveryStateToBind=$null)
    if($null -eq $ExpectedSnapshot -or !$ExpectedSnapshot.IsValid){throw "Refusing taskkill: initial bridge lineage is invalid."}
    Assert-PrimaryTaskRootAttestation -Snapshot $ExpectedSnapshot | Out-Null
    $quiescence=$null
    $killSucceeded=$false
    try {
        $quiescence=Enter-BridgeTreeDestructiveQuiescence -ExpectedSnapshot $ExpectedSnapshot
        $finalSnapshot=$quiescence.Snapshot
        if($null -eq $finalSnapshot -or !$finalSnapshot.IsValid){throw "Refusing taskkill: destructive-quiescence snapshot invalid."}
        $record=Convert-SnapshotRecord $finalSnapshot
        if($null -ne $RecoveryStateToBind){
            $RecoveryStateToBind.old_snapshot=$record
            $RecoveryStateToBind.old_snapshot_sha256=[string]$record.snapshot_sha256
            Save-RecoveryState $RecoveryStateToBind
        }
        $result=[pscustomobject]@{RootPid=[int]$finalSnapshot.TaskRoot.ProcessId;ProcessIds=@($finalSnapshot.Processes|ForEach-Object{[int]$_.ProcessId});Snapshot=$record}
        Assert-PrimaryTaskRootAttestation -Snapshot $finalSnapshot | Out-Null
        Invoke-TaskKillTree -ProcessId $result.RootPid
        $killSucceeded=$true
        return $result
    } finally {
        if($null -ne $quiescence){Release-BridgeTreeDestructiveQuiescence -Quiescence $quiescence -Resume (!$killSucceeded)}
    }
}


function Get-ProcessRecordIdentityKey {
    param($Record)
    if ($null -eq $Record) { throw "Missing process snapshot record." }
    $pidValue=[int]$Record.process_id; $ppidValue=[int]$Record.parent_process_id; $ticks=[long]$Record.creation_utc_ticks
    $exe=[string]$Record.executable_path; $cmd=[string]$Record.command_line
    if($pidValue -le 0 -or $ppidValue -lt 0 -or $ticks -le 0 -or [string]::IsNullOrWhiteSpace($exe) -or [string]::IsNullOrWhiteSpace($cmd)){throw "Invalid process snapshot record identity."}
    $separator=[char]0
    return ($pidValue.ToString()+$separator+$ppidValue.ToString()+$separator+$ticks.ToString()+$separator+$exe.ToLowerInvariant()+$separator+$cmd)
}

function Get-SnapshotRecordIdentity {
    param($Record)
    if($null -eq $Record -or $null -eq $Record.task_action -or $null -eq $Record.processes){throw "Snapshot record incomplete."}
    $execute=[string]$Record.task_action.execute; $arguments=[string]$Record.task_action.arguments; $working=[string]$Record.task_action.working_directory
    if([string]::IsNullOrWhiteSpace($execute)-or[string]::IsNullOrWhiteSpace($working)){throw "Snapshot task action incomplete."}
    $separator=[char]0
    $taskKey=$execute.ToLowerInvariant()+$separator+$arguments+$separator+$working.ToLowerInvariant()
    $processKeys=@($Record.processes|Sort-Object {[int]$_.process_id}|ForEach-Object{Get-ProcessRecordIdentityKey $_})
    if($processKeys.Count -eq 0){throw "Snapshot record has no process identities."}
    return ($taskKey+"`n"+($processKeys -join "`n"))
}

function Assert-SnapshotRecordIntegrity {
    param($Record)
    if($null -eq $Record){throw "Snapshot record missing."}
    Assert-NoUnexpectedProperties $Record @("task_action","root_pid","root_creation_utc_ticks","processes","snapshot_sha256") "Snapshot record"
    Assert-NoUnexpectedProperties $Record.task_action @("execute","arguments","working_directory") "Snapshot task action"
    if($Record.processes -is [string] -or $Record.processes -isnot [System.Collections.IEnumerable]){throw "Snapshot processes must be a JSON array."}
    foreach($field in @("execute","arguments","working_directory")){if($Record.task_action.$field -isnot [string]){throw("Snapshot task action "+$field+" must be a JSON string.")}}
    if(!(Test-JsonInteger $Record.root_pid) -or !(Test-JsonInteger $Record.root_creation_utc_ticks)){throw "Snapshot root identity types invalid."}
    foreach($p in @($Record.processes)){
        Assert-NoUnexpectedProperties $p @("process_id","parent_process_id","creation_utc_ticks","executable_path","command_line") "Snapshot process record"
        if(!(Test-JsonInteger $p.process_id) -or !(Test-JsonInteger $p.parent_process_id) -or !(Test-JsonInteger $p.creation_utc_ticks)){throw "Snapshot process numeric identity types invalid."}
        if($p.executable_path -isnot [string] -or $p.command_line -isnot [string]){throw "Snapshot process path/command types invalid."}
    }
    if($Record.snapshot_sha256 -isnot [string] -or [string]$Record.snapshot_sha256 -notmatch '^[0-9a-f]{64}$'){throw "Snapshot record hash missing/invalid."}
    $expectedAction=Get-ExpectedBridgeTaskActionIdentity
    if([string]$Record.task_action.execute -ine [string]$expectedAction.Execute -or [string]$Record.task_action.arguments -cne [string]$expectedAction.Arguments -or [string]$Record.task_action.working_directory -ine [string]$expectedAction.WorkingDirectory){throw "Stored snapshot task action drift/tamper."}
    $records=@($Record.processes)
    $byPid=@{}
    foreach($p in $records){$null=Get-ProcessRecordIdentityKey $p; $pidValue=[int]$p.process_id; if($byPid.ContainsKey($pidValue)){throw "Stored snapshot duplicate PID."};$byPid[$pidValue]=$p}
    $roots=@($records|Where-Object{!$byPid.ContainsKey([int]$_.parent_process_id)})
    if($roots.Count -ne 1){throw "Stored snapshot root cardinality invalid."}
    $root=$roots[0]
    if([int]$root.process_id -ne [int]$Record.root_pid -or [long]$root.creation_utc_ticks -ne [long]$Record.root_creation_utc_ticks){throw "Stored snapshot root binding invalid."}
    $queue=New-Object System.Collections.Generic.Queue[object];$visited=@{};$visited[[int]$root.process_id]=$true;$queue.Enqueue($root)
    while($queue.Count -gt 0){$parent=$queue.Dequeue();foreach($child in @($records|Where-Object{[int]$_.parent_process_id -eq [int]$parent.process_id})){if($visited.ContainsKey([int]$child.process_id)){throw "Stored snapshot cycle detected."};if([long]$parent.creation_utc_ticks -ge [long]$child.creation_utc_ticks){throw "Stored snapshot stale parent edge."};$visited[[int]$child.process_id]=$true;$queue.Enqueue($child)}}
    if($visited.Count -ne $records.Count){throw "Stored snapshot disconnected lineage."}
    $computed=Get-StringSha256 (Get-SnapshotRecordIdentity $Record)
    if($computed -cne [string]$Record.snapshot_sha256){throw "Stored snapshot canonical hash mismatch."}
}

function Convert-SnapshotRecord {
    param($Snapshot)
    if($null -eq $Snapshot -or !$Snapshot.IsValid){throw "Cannot record invalid bridge snapshot."}
    $processes=@($Snapshot.Processes|Sort-Object ProcessId|ForEach-Object{[ordered]@{process_id=[int]$_.ProcessId;parent_process_id=[int]$_.ParentProcessId;creation_utc_ticks=[long]$_.CreationUtcTicks;executable_path=[string]$_.ExecutablePath;command_line=[string]$_.CommandLine}})
    $record=[ordered]@{task_action=[ordered]@{execute=[string]$Snapshot.TaskAction.Execute;arguments=[string]$Snapshot.TaskAction.Arguments;working_directory=[string]$Snapshot.TaskAction.WorkingDirectory};root_pid=[int]$Snapshot.TaskRoot.ProcessId;root_creation_utc_ticks=[long]$Snapshot.TaskRoot.CreationUtcTicks;processes=$processes;snapshot_sha256=$null}
    $record.snapshot_sha256=Get-StringSha256 (Get-SnapshotRecordIdentity $record)
    Assert-SnapshotRecordIntegrity -Record $record
    return $record
}

function Test-OldTreeAbsent {
    param($OldSnapshotRecord)
    try { Assert-SnapshotRecordIntegrity -Record $OldSnapshotRecord } catch { return $false }
    foreach($old in @($OldSnapshotRecord.processes)) {
        try {
            $now=Get-BridgeProcessById -ProcessId ([int]$old.process_id)
            if($null -ne $now){$oldKey=Get-ProcessRecordIdentityKey $old;if([string]::Equals([string]$now.IdentityKey,[string]$oldKey,[System.StringComparison]::Ordinal)){return $false}}
        } catch { return $false }
    }
    return $true
}

function Stop-BridgeProcesses {
    param($TaskAction)
    $taskAction = $TaskAction
    if ($null -eq $taskAction) { $taskAction = Get-BridgeTaskActionIdentity }
    $procs = @(Get-BridgeProcesses -TaskAction $taskAction)
    if ($procs.Count -eq 0) { return $null }
    $snapshot = New-BridgeProcessSnapshot -Processes $procs -TaskAction $taskAction
    if (!$snapshot.IsValid) { throw ("Refusing maintenance cleanup: invalid bridge lineage; " + (@($snapshot.FailureReasons) -join ",")) }
    $result = Invoke-ValidatedBridgeTreeKill -ExpectedSnapshot $snapshot
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(5)
    while ([DateTimeOffset]::UtcNow -lt $deadline -and !(Test-OldTreeAbsent $result.Snapshot)) { Start-Sleep -Milliseconds 250 }
    if (!(Test-OldTreeAbsent $result.Snapshot)) { throw "Maintenance cleanup could not prove complete old bridge tree absent." }
    Log ("Validated bridge tree removed root_pid=" + $result.RootPid + " nodes=" + @($result.ProcessIds).Count)
    return $result.Snapshot
}


function Assert-InstallRollbackWriterQuiescence {
    param([string]$ConfigHash)

    # Rollback authority is disabled before any task/process stop. This prevents a
    # recovery instance that races the installer catch path from logically restarting
    # the primary after the serialization lock is released.
    Write-DesiredState -Desired "STOPPED" -ConfigHash $ConfigHash
    $desiredRollback = Read-Json $DesiredStatePath
    if ($null -eq $desiredRollback -or [string]$desiredRollback.desired_state -cne "STOPPED" -or [string]$desiredRollback.config_sha256 -cne $ConfigHash) {
        throw "Rollback STOPPED desired-state publication/read-back failed."
    }

    $recoveryTask = Get-ScheduledTask -TaskName $RecoveryTaskName -ErrorAction SilentlyContinue
    if ($null -ne $recoveryTask -and $recoveryTask.State.ToString() -eq "Running") {
        Stop-ScheduledTask -TaskName $RecoveryTaskName -ErrorAction Stop
    }
    $primaryTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $primaryTask -and $primaryTask.State.ToString() -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    }

    $expectedAction = Get-ExpectedBridgeTaskActionIdentity
    $null = Stop-BridgeProcesses -TaskAction $expectedAction

    # Require a bounded stable quiescent window, not one instantaneous sample. Any
    # task/process ambiguity or restart attempt keeps rollback fail-closed.
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(5)
    $stableSamples = 0
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        $primaryNow = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $recoveryNow = Get-ScheduledTask -TaskName $RecoveryTaskName -ErrorAction SilentlyContinue
        $taskRunning = (($null -ne $primaryNow -and $primaryNow.State.ToString() -eq "Running") -or ($null -ne $recoveryNow -and $recoveryNow.State.ToString() -eq "Running"))
        $writers = @(Get-BridgeProcesses -TaskAction $expectedAction)
        if (!$taskRunning -and $writers.Count -eq 0) {
            $stableSamples++
            if ($stableSamples -ge 3) {
                Log "Rollback writer/task quiescence PASS desired_state=STOPPED stable_samples=3"
                return
            }
        }
        else {
            $stableSamples = 0
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Rollback writer/task quiescence could not be proven."
}

function Read-Json([string]$Path) {
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $stream=$null
    try {
        # Read+Delete share permits atomic rename/replace while denying in-place writers;
        # the already-open handle remains a stable size-check + read object.
        $share=[System.IO.FileShare]::Read -bor [System.IO.FileShare]::Delete
        $stream=[System.IO.File]::Open($Path,[System.IO.FileMode]::Open,[System.IO.FileAccess]::Read,$share)
        if([long]$stream.Length -gt 8388608){throw ("JSON file exceeds 8 MiB safety limit: "+$Path)}
        $length=[int]$stream.Length
        $bytes=New-Object byte[] $length
        $offset=0
        while($offset -lt $length){
            $read=$stream.Read($bytes,$offset,$length-$offset)
            if($read -le 0){throw ("Unexpected EOF while reading JSON file: "+$Path)}
            $offset += $read
        }
    } finally {
        if($null -ne $stream){$stream.Dispose()}
    }
    $text=$Utf8NoBom.GetString($bytes)
    if($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF){$text=$text.Substring(1)}
    return ($text | ConvertFrom-Json -ErrorAction Stop)
}

function Get-FileSha256([string]$Path) {
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw ("Missing hash target: " + $Path)
    }
    return ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant())
}

function Get-StringSha256([string]$Text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        $hash = $sha.ComputeHash($bytes)
        return (($hash | ForEach-Object { $_.ToString("x2") }) -join "")
    }
    finally {
        $sha.Dispose()
    }
}

function Parse-Utc([object]$Value, [string]$Field) {
    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        throw ("Missing/invalid timestamp field type: " + $Field)
    }
    $text=[string]$Value
    if($text -notmatch '(?:Z|[+-][0-9]{2}:[0-9]{2})$'){throw ("Timestamp must carry an explicit UTC/offset suffix: " + $Field)}
    try {
        return ([DateTimeOffset]::Parse($text,[System.Globalization.CultureInfo]::InvariantCulture,[System.Globalization.DateTimeStyles]::None)).ToUniversalTime()
    }
    catch {
        throw ("Invalid timestamp field: " + $Field)
    }
}

function Write-JsonAtomic([string]$Path, $Object) {
    $parent = Split-Path $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $tmp = $Path + "." + $PID + "." + [Guid]::NewGuid().ToString("N") + ".tmp"
    $json = $Object | ConvertTo-Json -Depth 16
    $bytes=$Utf8NoBom.GetBytes($json + "`r`n")
    $stream=$null
    try {
        $stream=[System.IO.File]::Open($tmp,[System.IO.FileMode]::CreateNew,[System.IO.FileAccess]::Write,[System.IO.FileShare]::None)
        $stream.Write($bytes,0,$bytes.Length)
        $stream.Flush($true)
    } finally {
        if($null -ne $stream){$stream.Dispose()}
    }
    $backup = $Path + "." + $PID + "." + [Guid]::NewGuid().ToString("N") + ".bak"
    try {
        if(Test-Path -LiteralPath $Path -PathType Leaf){[System.IO.File]::Replace($tmp,$Path,$backup,$true)}
        else{[System.IO.File]::Move($tmp,$Path)}
    }
    finally {
        if(Test-Path -LiteralPath $tmp){Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue}
        if(Test-Path -LiteralPath $backup){Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue}
    }
}

function Resolve-UserSid([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    try {
        if ($Value -match '^S-1-') {
            return (New-Object System.Security.Principal.SecurityIdentifier($Value)).Value
        }
        $account = New-Object System.Security.Principal.NTAccount($Value)
        return ($account.Translate([System.Security.Principal.SecurityIdentifier])).Value
    }
    catch { return $null }
}


function Assert-TaskSecurityDescriptor {
    param([string]$Sddl, [string]$ExpectedUser)
    $currentSid=Resolve-UserSid $ExpectedUser
    if([string]::IsNullOrWhiteSpace($currentSid)){throw("Cannot resolve expected task user SID for ACL validation: "+$ExpectedUser)}
    if([string]::IsNullOrWhiteSpace($Sddl)){throw "Task Scheduler returned an empty security descriptor."}
    $raw=New-Object System.Security.AccessControl.RawSecurityDescriptor($Sddl)
    if($null -eq $raw.DiscretionaryAcl){throw "Task security descriptor has a NULL/missing DACL."}
    $ownerSid=if($null -ne $raw.Owner){[string]$raw.Owner.Value}else{$null}
    $trustedMutationSids=@($currentSid,"S-1-5-18","S-1-5-32-544")
    if($ownerSid -notin $trustedMutationSids){throw("Task owner SID is outside the reviewed boundary: "+$ownerSid)}
    $readOnlyMask=[uint32]0x00120089
    $hasCurrent=$false
    $hasSystem=$false
    foreach($ace in @($raw.DiscretionaryAcl)){
        if([string]$ace.AceType -cne "AccessAllowed"){throw("Task DACL contains an unreviewed ACE type: "+[string]$ace.AceType)}
        if($null -eq $ace.SecurityIdentifier){throw "Task DACL ACE lacks a SID."}
        $sid=[string]$ace.SecurityIdentifier.Value
        $mask=[uint32]$ace.AccessMask
        if([string]::Equals($sid,$currentSid,[System.StringComparison]::OrdinalIgnoreCase)){$hasCurrent=$true}
        if($sid -ceq "S-1-5-18"){$hasSystem=$true}
        if($sid -notin $trustedMutationSids){
            if(([uint32]($mask -bor $readOnlyMask)) -ne $readOnlyMask){
                throw("Task DACL grants non-read-only rights outside the reviewed principals: sid="+$sid+" mask=0x"+$mask.ToString("X8"))
            }
        }
    }
    if(!$hasCurrent){throw "Task DACL does not contain an explicit current-user ACE."}
    if(!$hasSystem){throw "Task DACL does not contain the required SYSTEM ACE."}
    $sections=[System.Security.AccessControl.AccessControlSections]::Owner -bor [System.Security.AccessControl.AccessControlSections]::Group -bor [System.Security.AccessControl.AccessControlSections]::Access
    $canonical=[string]$raw.GetSddlForm($sections)
    return [pscustomobject]@{CanonicalSddl=$canonical;Sha256=(Get-StringSha256 $canonical);OwnerSid=$ownerSid}
}

function Get-TaskSecurityBinding {
    param([string]$RegisteredTaskName, [string]$ExpectedUser)
    try {
        $service=New-Object -ComObject "Schedule.Service"
        $service.Connect()
        $task=$service.GetFolder("\").GetTask($RegisteredTaskName)
        return (Assert-TaskSecurityDescriptor -Sddl ([string]$task.GetSecurityDescriptor(7)) -ExpectedUser $ExpectedUser)
    }
    catch { throw("Task security-descriptor validation failed for "+$RegisteredTaskName+": "+$_.Exception.Message) }
}

function Test-SameUser([string]$Left, [string]$Right) {
    $leftSid = Resolve-UserSid $Left
    $rightSid = Resolve-UserSid $Right
    if ($null -eq $leftSid -or $null -eq $rightSid) { return $false }
    return [string]::Equals($leftSid, $rightSid, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-ObjectPropertyNames {
    param($Object)
    if ($null -eq $Object) { return @() }
    if ($Object -is [System.Collections.IDictionary]) { return @($Object.Keys | ForEach-Object { [string]$_ }) }
    return @($Object.PSObject.Properties | ForEach-Object { [string]$_.Name })
}

function Assert-NoUnexpectedProperties {
    param($Object, [string[]]$Allowed, [string]$Context)
    if ($null -eq $Object) { throw ($Context + " is missing.") }
    $allowedSet = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::Ordinal)
    foreach ($name in $Allowed) { [void]$allowedSet.Add([string]$name) }
    foreach ($name in @(Get-ObjectPropertyNames $Object)) {
        if (!$allowedSet.Contains([string]$name)) { throw ($Context + " contains unexpected property: " + [string]$name) }
    }
}


function Test-JsonInteger {
    param($Value)
    if($null -eq $Value -or $Value -is [bool]){return $false}
    return ($Value -is [byte] -or $Value -is [sbyte] -or $Value -is [int16] -or $Value -is [uint16] -or $Value -is [int32] -or $Value -is [uint32] -or $Value -is [int64] -or $Value -is [uint64])
}

function Assert-JsonStringOrNull {
    param($Value,[string]$Field)
    if($null -ne $Value -and $Value -isnot [string]){throw($Field+" must be a JSON string or null.")}
}

function Test-Uuid([object]$Value) {
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) { return $false }
    $parsed = [Guid]::Empty
    return [Guid]::TryParse([string]$Value, [ref]$parsed)
}

function Assert-RecoveryInputs {
    foreach ($required in @($RecoveryScript, $RecoveryConfig, $RecoveryTaskSpec, $PrimaryTaskSpec, $PowerShellExe)) {
        if (!(Test-Path -LiteralPath $required -PathType Leaf)) {
            throw ("Missing recovery candidate/runtime file: " + $required)
        }
    }
    $config = Read-Json $RecoveryConfig
    Assert-NoUnexpectedProperties $config @(
        "schema_version","primary_task_name","recovery_task_name","expected_bridge_version","check_interval_seconds","f3_threshold_seconds",
        "freshness_seconds","acceptance_deadline_seconds","old_tree_absence_wait_seconds","post_start_observation_seconds","prior_healthy_max_age_seconds",
        "bridge_sha256","recovery_script_sha256","primary_task_definition_sha256","recovery_task_definition_sha256",
        "local_recovery_state_directory","desired_state_file","recovery_state_file","lock_file"
    ) "Recovery config"
    if ($null -eq $config -or !(Test-JsonInteger $config.schema_version) -or [int]$config.schema_version -ne 1) { throw "Recovery config missing/schema type invalid." }
    foreach($field in @("check_interval_seconds","f3_threshold_seconds","freshness_seconds","acceptance_deadline_seconds","old_tree_absence_wait_seconds","post_start_observation_seconds","prior_healthy_max_age_seconds")){if(!(Test-JsonInteger $config.$field)){throw("Recovery config integer field type invalid: "+$field)}}
    foreach($field in @("primary_task_name","recovery_task_name","expected_bridge_version","bridge_sha256","recovery_script_sha256","primary_task_definition_sha256","recovery_task_definition_sha256","local_recovery_state_directory","desired_state_file","recovery_state_file","lock_file")){if($config.$field -isnot [string]){throw("Recovery config string field type invalid: "+$field)}}
    if ([string]$config.primary_task_name -cne $TaskName -or [string]$config.recovery_task_name -cne $RecoveryTaskName) { throw "Recovery config task names mismatch." }
    if ([string]$config.expected_bridge_version -cne $ExpectedVer) { throw "Recovery config bridge version mismatch." }
    if ((Get-FileSha256 $Bridge) -cne [string]$config.bridge_sha256) { throw "Bridge hash does not match frozen recovery config." }
    if ((Get-FileSha256 $RecoveryScript) -cne [string]$config.recovery_script_sha256) { throw "Recovery script hash does not match frozen recovery config." }
    if ((Get-FileSha256 $RecoveryTaskSpec) -cne [string]$config.recovery_task_definition_sha256) { throw "Recovery task spec hash does not match frozen recovery config." }
    if ((Get-FileSha256 $PrimaryTaskSpec) -cne [string]$config.primary_task_definition_sha256) { throw "Primary task spec hash does not match frozen recovery config." }
    return $config
}

function Write-DesiredState([string]$Desired, [string]$ConfigHash) {
    if ($Desired -notin @("RUNNING", "STOPPED", "MAINTENANCE")) { throw "Invalid desired state." }
    Write-JsonAtomic -Path $DesiredStatePath -Object ([ordered]@{
        schema_version = 1
        config_sha256 = $ConfigHash
        desired_state = $Desired
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
    })
}

function Get-NullableString([object]$Value) {
    if ($null -eq $Value) { return $null }
    return [string]$Value
}

function Get-RecoveryStateIntegrity {
    param($State)

    $lastHealthy = $null
    if ($null -ne $State.last_healthy) {
        $lastHealthy = [ordered]@{
            incarnation_id = Get-NullableString $State.last_healthy.incarnation_id
            started_at = Get-NullableString $State.last_healthy.started_at
            observed_at = Get-NullableString $State.last_healthy.observed_at
            snapshot_sha256 = Get-NullableString $State.last_healthy.snapshot_sha256
        }
    }

    $deadlineAnchorEvidence = $null
    if ($null -ne $State.deadline_anchor_evidence) {
        $deadlineAnchorEvidence = [ordered]@{
            schema_version = [int]$State.deadline_anchor_evidence.schema_version
            incarnation_id = Get-NullableString $State.deadline_anchor_evidence.incarnation_id
            process_pid = [int]$State.deadline_anchor_evidence.process_pid
            started_at = Get-NullableString $State.deadline_anchor_evidence.started_at
            last_healthy_observed_at = Get-NullableString $State.deadline_anchor_evidence.last_healthy_observed_at
            heartbeat_at = Get-NullableString $State.deadline_anchor_evidence.heartbeat_at
            health_at = Get-NullableString $State.deadline_anchor_evidence.health_at
        }
    }

    $canonical = [ordered]@{
        schema_version = [int]$State.schema_version
        config_sha256 = Get-NullableString $State.config_sha256
        mode = Get-NullableString $State.mode
        episode_id = Get-NullableString $State.episode_id
        episode_reason = Get-NullableString $State.episode_reason
        failure_boundary = Get-NullableString $State.failure_boundary
        deadline_anchor_at = Get-NullableString $State.deadline_anchor_at
        deadline_anchor_source = Get-NullableString $State.deadline_anchor_source
        deadline_anchor_evidence = $deadlineAnchorEvidence
        acceptance_deadline_at = Get-NullableString $State.acceptance_deadline_at
        start_attempted = [bool]$State.start_attempted
        start_attempt_at = Get-NullableString $State.start_attempt_at
        old_incarnation_id = Get-NullableString $State.old_incarnation_id
        old_snapshot_sha256 = Get-NullableString $State.old_snapshot_sha256
        accepted_incarnation_id = Get-NullableString $State.accepted_incarnation_id
        primary_task_semantic_sha256 = Get-NullableString $State.primary_task_semantic_sha256
        recovery_task_semantic_sha256 = Get-NullableString $State.recovery_task_semantic_sha256
        primary_task_security_sha256 = Get-NullableString $State.primary_task_security_sha256
        recovery_task_security_sha256 = Get-NullableString $State.recovery_task_security_sha256
        health_missing_incarnation_id = Get-NullableString $State.health_missing_incarnation_id
        health_missing_since = Get-NullableString $State.health_missing_since
        last_healthy = $lastHealthy
        last_successful_episode_id = Get-NullableString $State.last_successful_episode_id
        failure_detail = Get-NullableString $State.failure_detail
        updated_at = Get-NullableString $State.updated_at
    }
    return (Get-StringSha256 (($canonical | ConvertTo-Json -Depth 12 -Compress)))
}

function Assert-RecoveryState {
    param($State, [string]$ConfigHash)

    Assert-NoUnexpectedProperties $State @(
        "schema_version","config_sha256","mode","episode_id","episode_reason","failure_boundary","deadline_anchor_at","deadline_anchor_source",
        "deadline_anchor_evidence","acceptance_deadline_at","start_attempted","start_attempt_at","old_incarnation_id","old_snapshot_sha256","old_snapshot",
        "accepted_incarnation_id","primary_task_semantic_sha256","recovery_task_semantic_sha256","primary_task_security_sha256","recovery_task_security_sha256","health_missing_incarnation_id","health_missing_since",
        "last_healthy","last_successful_episode_id","failure_detail","updated_at","state_sha256"
    ) "Recovery state"
    if($null -ne $State.last_healthy){Assert-NoUnexpectedProperties $State.last_healthy @("incarnation_id","started_at","observed_at","snapshot_sha256","snapshot") "last_healthy"}
    if($null -ne $State.deadline_anchor_evidence){Assert-NoUnexpectedProperties $State.deadline_anchor_evidence @("schema_version","incarnation_id","process_pid","started_at","last_healthy_observed_at","heartbeat_at","health_at") "deadline_anchor_evidence"}
    if ($null -eq $State -or !(Test-JsonInteger $State.schema_version) -or [int]$State.schema_version -ne 5) { throw "Recovery-state schema/type mismatch." }
    if($State.config_sha256 -isnot [string] -or $State.mode -isnot [string] -or $State.state_sha256 -isnot [string]){throw "Recovery-state core field types invalid."}
    if($State.start_attempted -isnot [bool]){throw "Recovery-state start_attempted must be boolean."}
    foreach($field in @("episode_id","episode_reason","failure_boundary","deadline_anchor_at","deadline_anchor_source","acceptance_deadline_at","start_attempt_at","old_incarnation_id","old_snapshot_sha256","accepted_incarnation_id","primary_task_semantic_sha256","recovery_task_semantic_sha256","primary_task_security_sha256","recovery_task_security_sha256","health_missing_incarnation_id","health_missing_since","last_successful_episode_id","failure_detail","updated_at")){Assert-JsonStringOrNull $State.$field ("recovery_state."+$field)}
    if($null -ne $State.last_healthy){
        foreach($field in @("incarnation_id","started_at","observed_at","snapshot_sha256")){if($State.last_healthy.$field -isnot [string]){throw("recovery_state.last_healthy."+$field+" must be a JSON string.")}}
    }
    if($null -ne $State.deadline_anchor_evidence){
        if(!(Test-JsonInteger $State.deadline_anchor_evidence.schema_version) -or !(Test-JsonInteger $State.deadline_anchor_evidence.process_pid)){throw "deadline_anchor_evidence numeric field types invalid."}
        foreach($field in @("incarnation_id","started_at","last_healthy_observed_at","heartbeat_at","health_at")){if($State.deadline_anchor_evidence.$field -isnot [string]){throw("deadline_anchor_evidence."+$field+" must be a JSON string.")}}
    }
    if ([string]$State.config_sha256 -cne $ConfigHash) { throw "Recovery-state config binding mismatch." }
    if ([string]$State.state_sha256 -notmatch '^[0-9a-f]{64}$') { throw "Recovery-state integrity hash missing/invalid." }
    if ((Get-RecoveryStateIntegrity $State) -cne [string]$State.state_sha256) { throw "Recovery-state integrity mismatch." }

    $mode = [string]$State.mode
    if ($mode -notin @("READY", "RECOVERING", "RECOVERY_FAILED")) { throw "Recovery-state mode invalid." }
    $primaryTaskHash = Get-NullableString $State.primary_task_semantic_sha256
    $recoveryTaskHash = Get-NullableString $State.recovery_task_semantic_sha256
    $primarySecurityHash = Get-NullableString $State.primary_task_security_sha256
    $recoverySecurityHash = Get-NullableString $State.recovery_task_security_sha256
    $hasPrimaryTaskHash = ![string]::IsNullOrWhiteSpace($primaryTaskHash)
    $hasRecoveryTaskHash = ![string]::IsNullOrWhiteSpace($recoveryTaskHash)
    $hasPrimarySecurityHash = ![string]::IsNullOrWhiteSpace($primarySecurityHash)
    $hasRecoverySecurityHash = ![string]::IsNullOrWhiteSpace($recoverySecurityHash)
    if (($hasPrimaryTaskHash -ne $hasRecoveryTaskHash) -or ($hasPrimarySecurityHash -ne $hasRecoverySecurityHash) -or ($hasPrimaryTaskHash -ne $hasPrimarySecurityHash)) { throw "Recovery-state task semantic/security hash binding is partial." }
    if ($hasPrimaryTaskHash -and ($primaryTaskHash -notmatch '^[0-9a-f]{64}$' -or $recoveryTaskHash -notmatch '^[0-9a-f]{64}$' -or $primarySecurityHash -notmatch '^[0-9a-f]{64}$' -or $recoverySecurityHash -notmatch '^[0-9a-f]{64}$')) { throw "Recovery-state task semantic/security hash invalid." }
    $updated = Parse-Utc $State.updated_at "recovery_state.updated_at"
    if ($updated -gt [DateTimeOffset]::UtcNow.AddMinutes(5)) { throw "Recovery-state timestamp is in the future." }

    $missingSinceValue = $State.health_missing_since
    $missingIncValue = $State.health_missing_incarnation_id
    $hasMissingSince = $null -ne $missingSinceValue -and ![string]::IsNullOrWhiteSpace([string]$missingSinceValue)
    $hasMissingInc = $null -ne $missingIncValue -and ![string]::IsNullOrWhiteSpace([string]$missingIncValue)
    if ($hasMissingSince -ne $hasMissingInc) { throw "Recovery-state missing-HEALTH tracker is partial/tampered." }
    if ($hasMissingSince) {
        if (!(Test-Uuid $missingIncValue)) { throw "Recovery-state missing-HEALTH incarnation invalid." }
        $missingSince = Parse-Utc $missingSinceValue "recovery_state.health_missing_since"
        if ($missingSince -gt [DateTimeOffset]::UtcNow.AddMinutes(5)) { throw "Recovery-state missing-HEALTH timestamp is in the future." }
    }

    if ($null -ne $State.last_healthy) {
        if (!(Test-Uuid $State.last_healthy.incarnation_id)) { throw "last_healthy incarnation invalid." }
        $lastStarted = Parse-Utc $State.last_healthy.started_at "recovery_state.last_healthy.started_at"
        $lastObserved = Parse-Utc $State.last_healthy.observed_at "recovery_state.last_healthy.observed_at"
        if ($lastObserved -lt $lastStarted -or $lastObserved -gt [DateTimeOffset]::UtcNow.AddMinutes(5)) { throw "last_healthy timestamp ordering invalid." }
        if ([string]$State.last_healthy.snapshot_sha256 -notmatch '^[0-9a-f]{64}$') { throw "last_healthy snapshot hash invalid." }
        Assert-SnapshotRecordIntegrity -Record $State.last_healthy.snapshot
        if ([string]$State.last_healthy.snapshot.snapshot_sha256 -cne [string]$State.last_healthy.snapshot_sha256) { throw "last_healthy snapshot binding mismatch." }
    }
    if ($null -ne $State.last_successful_episode_id -and ![string]::IsNullOrWhiteSpace([string]$State.last_successful_episode_id) -and !(Test-Uuid $State.last_successful_episode_id)) { throw "last_successful_episode_id invalid." }

    if ($mode -eq "READY") {
        foreach ($field in @("episode_id", "episode_reason", "failure_boundary", "deadline_anchor_at", "deadline_anchor_source", "deadline_anchor_evidence", "acceptance_deadline_at", "start_attempt_at", "old_incarnation_id", "old_snapshot_sha256", "old_snapshot", "failure_detail")) {
            if ($null -ne $State.$field -and ![string]::IsNullOrWhiteSpace([string]$State.$field)) { throw ("READY state forbids field " + $field) }
        }
        if ([bool]$State.start_attempted) { throw "READY state cannot have start_attempted=true." }
        $hasAccepted = $null -ne $State.accepted_incarnation_id -and ![string]::IsNullOrWhiteSpace([string]$State.accepted_incarnation_id)
        $hasLastHealthy = $null -ne $State.last_healthy
        if ($hasAccepted -ne $hasLastHealthy) { throw "READY accepted_incarnation_id/last_healthy baseline must be present or absent together." }
        if ($hasLastHealthy -and !$hasPrimaryTaskHash) { throw "READY last_healthy baseline requires exact task semantic hashes." }
        if ($hasAccepted) {
            if (!(Test-Uuid $State.accepted_incarnation_id)) { throw "READY accepted_incarnation_id invalid." }
            if ([string]$State.accepted_incarnation_id -cne [string]$State.last_healthy.incarnation_id) { throw "READY accepted incarnation not bound to last_healthy." }
        }
        if ($hasMissingSince -and ($null -eq $State.last_healthy -or [string]$State.last_healthy.incarnation_id -cne [string]$missingIncValue)) {
            throw "READY missing-HEALTH tracker not bound to last_healthy incarnation."
        }
        return
    }

    if (!$hasPrimaryTaskHash) { throw ($mode + " state requires exact task semantic hashes.") }
    if (!(Test-Uuid $State.episode_id)) { throw ($mode + " state missing valid episode_id.") }
    if ([string]$State.episode_reason -notin @("F1_OR_F2_ABSENT", "F3_LIVENESS")) { throw ($mode + " episode_reason invalid.") }
    $boundary = Parse-Utc $State.failure_boundary "recovery_state.failure_boundary"
    if ($updated -lt $boundary) { throw ($mode + " updated_at predates failure_boundary.") }
    $anchor = Parse-Utc $State.deadline_anchor_at "recovery_state.deadline_anchor_at"
    $anchorSource=[string]$State.deadline_anchor_source
    $deadline = Parse-Utc $State.acceptance_deadline_at "recovery_state.acceptance_deadline_at"
    if($anchor -gt $boundary){throw ($mode + " deadline anchor postdates failure_boundary.")}
    if ([string]$State.episode_reason -ceq "F3_LIVENESS") {
        if($anchorSource -cne "F3_FAILURE_BOUNDARY" -or $anchor -ne $boundary){throw ($mode + " F3 deadline anchor binding invalid.")}
        if($null -ne $State.deadline_anchor_evidence){throw ($mode + " F3 must not carry F1/F2 deadline evidence.")}
        if ($deadline -ne $boundary.AddSeconds(90)) { throw ($mode + " F3 acceptance deadline must equal failure_boundary + 90s.") }
    } else {
        if($anchorSource -cne "LAST_TRUSTED_OLD_LIVENESS"){throw ($mode + " F1/F2 deadline anchor source invalid.")}
        $e=$State.deadline_anchor_evidence
        if($null -eq $e -or [int]$e.schema_version -ne 1){throw ($mode + " F1/F2 deadline anchor evidence missing/invalid.")}
        if([string]$e.incarnation_id -cne [string]$State.last_healthy.incarnation_id -or [string]$e.incarnation_id -cne [string]$State.old_incarnation_id){throw ($mode + " F1/F2 deadline evidence incarnation binding mismatch.")}
        if([int]$e.process_pid -ne [int]$State.old_snapshot.root_pid){throw ($mode + " F1/F2 deadline evidence root PID binding mismatch.")}
        $eStarted=Parse-Utc $e.started_at "recovery_state.deadline_anchor_evidence.started_at"
        $eObserved=Parse-Utc $e.last_healthy_observed_at "recovery_state.deadline_anchor_evidence.last_healthy_observed_at"
        $eHeartbeat=Parse-Utc $e.heartbeat_at "recovery_state.deadline_anchor_evidence.heartbeat_at"
        $eHealth=Parse-Utc $e.health_at "recovery_state.deadline_anchor_evidence.health_at"
        if($eStarted -ne (Parse-Utc $State.last_healthy.started_at "recovery_state.last_healthy.started_at")){throw ($mode + " F1/F2 deadline evidence started_at binding mismatch.")}
        if($eObserved -ne (Parse-Utc $State.last_healthy.observed_at "recovery_state.last_healthy.observed_at")){throw ($mode + " F1/F2 deadline evidence healthy-observation binding mismatch.")}
        foreach($t in @($eObserved,$eHeartbeat,$eHealth)){if($t -lt $eStarted -or $t -gt $boundary){throw ($mode + " F1/F2 deadline evidence timestamp outside proven pre-failure interval.")}}
        $expectedAnchor=$eObserved
        foreach($t in @($eHeartbeat,$eHealth)){if($t -gt $expectedAnchor){$expectedAnchor=$t}}
        if($anchor -ne $expectedAnchor){throw ($mode + " F1/F2 deadline anchor does not recompute from persisted evidence.")}
        if ($deadline -ne $anchor.AddSeconds(90)) { throw ($mode + " F1/F2 acceptance deadline is not bound to the trusted pre-failure liveness anchor.") }
    }
    if (!(Test-Uuid $State.old_incarnation_id)) { throw ($mode + " old_incarnation_id invalid.") }
    if ($null -eq $State.last_healthy) { throw ($mode + " requires the validated prior last_healthy baseline.") }
    if (!(Test-Uuid $State.accepted_incarnation_id)) { throw ($mode + " accepted_incarnation_id baseline invalid.") }
    if ([string]$State.accepted_incarnation_id -cne [string]$State.last_healthy.incarnation_id -or [string]$State.old_incarnation_id -cne [string]$State.last_healthy.incarnation_id) { throw ($mode + " prior/accepted/old incarnation bindings disagree.") }
    if ([string]$State.old_snapshot_sha256 -notmatch '^[0-9a-f]{64}$') { throw ($mode + " old snapshot binding missing/invalid.") }
    Assert-SnapshotRecordIntegrity -Record $State.old_snapshot
    if ([string]$State.old_snapshot.snapshot_sha256 -cne [string]$State.old_snapshot_sha256) { throw ($mode + " old snapshot hash binding mismatch.") }
    if ([bool]$State.start_attempted) {
        $attemptAt = Parse-Utc $State.start_attempt_at "recovery_state.start_attempt_at"
        if ($attemptAt -lt $boundary -or $attemptAt -gt [DateTimeOffset]::UtcNow.AddMinutes(5)) { throw ($mode + " start_attempt_at outside valid episode interval.") }
    }
    elseif ($null -ne $State.start_attempt_at -and ![string]::IsNullOrWhiteSpace([string]$State.start_attempt_at)) {
        throw ($mode + " start_attempted=false requires null start_attempt_at.")
    }
    if ($hasMissingSince) { throw ($mode + " forbids active missing-HEALTH tracker.") }

    if ($mode -eq "RECOVERING") {
        if ($null -ne $State.failure_detail -and ![string]::IsNullOrWhiteSpace([string]$State.failure_detail)) { throw "RECOVERING forbids failure_detail." }
    }
    else {
        if ([string]::IsNullOrWhiteSpace([string]$State.failure_detail)) { throw "RECOVERY_FAILED requires failure_detail." }
    }
}

function New-BaseRecoveryState {
    param([string]$ConfigHash)
    $state = [ordered]@{
        schema_version = 5
        config_sha256 = $ConfigHash
        mode = "READY"
        episode_id = $null
        episode_reason = $null
        failure_boundary = $null
        deadline_anchor_at = $null
        deadline_anchor_source = $null
        deadline_anchor_evidence = $null
        acceptance_deadline_at = $null
        start_attempted = $false
        start_attempt_at = $null
        old_incarnation_id = $null
        old_snapshot_sha256 = $null
        old_snapshot = $null
        accepted_incarnation_id = $null
        primary_task_semantic_sha256 = $null
        recovery_task_semantic_sha256 = $null
        primary_task_security_sha256 = $null
        recovery_task_security_sha256 = $null
        health_missing_incarnation_id = $null
        health_missing_since = $null
        last_healthy = $null
        last_successful_episode_id = $null
        failure_detail = $null
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
        state_sha256 = $null
    }
    $state.state_sha256 = Get-RecoveryStateIntegrity $state
    return $state
}

function Save-RecoveryState {
    param($State)
    $State.updated_at = [DateTimeOffset]::UtcNow.ToString("o")
    $State.state_sha256 = Get-RecoveryStateIntegrity $State
    Write-JsonAtomic -Path $RecoveryStatePath -Object $State
}

function Initialize-RecoveryState([string]$ConfigHash) {
    $state = New-BaseRecoveryState -ConfigHash $ConfigHash
    Save-RecoveryState $state
    return $state
}

function Seed-RecoveryHealthyBaseline {
    param([string]$ConfigHash, $StatusObject, $TaskBinding)
    if ($null -eq $TaskBinding -or [string]$TaskBinding.PrimarySemanticSha256 -notmatch '^[0-9a-f]{64}$' -or [string]$TaskBinding.RecoverySemanticSha256 -notmatch '^[0-9a-f]{64}$' -or [string]$TaskBinding.PrimarySecuritySha256 -notmatch '^[0-9a-f]{64}$' -or [string]$TaskBinding.RecoverySecuritySha256 -notmatch '^[0-9a-f]{64}$') { throw "Cannot seed recovery baseline without exact task semantic and security fingerprints." }
    if ($null -eq $StatusObject -or !(Test-Uuid $StatusObject.incarnation_id)) { throw "Cannot seed recovery baseline from invalid status." }
    $snapshot = Get-BridgeProcessSnapshot
    if (!$snapshot.IsValid) { throw "Cannot seed recovery baseline from invalid process tree." }
    Assert-PrimaryTaskRootAttestation -Snapshot $snapshot | Out-Null
    $statusProcessMatches = @($snapshot.Processes | Where-Object { [int]$_.ProcessId -eq [int]$StatusObject.process_pid })
    if ($statusProcessMatches.Count -ne 1) { throw "Baseline status PID is not an exact member of the validated Scheduled Task process tree." }
    if ([string]$StatusObject.heartbeat_incarnation_id -cne [string]$StatusObject.incarnation_id -or [string]$StatusObject.health_incarnation_id -cne [string]$StatusObject.incarnation_id -or [string]$StatusObject.health_status -cne "OK") { throw "Baseline status liveness binding invalid." }
    $started = (Parse-Utc $StatusObject.started_at "baseline.started_at")
    $heartbeat = (Parse-Utc $StatusObject.heartbeat_at "baseline.heartbeat_at")
    $health = (Parse-Utc $StatusObject.health_at "baseline.health_at")
    $now = [DateTimeOffset]::UtcNow
    if($heartbeat -lt $started -or $health -lt $started){throw "Baseline status liveness timestamps predate started_at."}
    if (($now-$heartbeat).TotalSeconds -lt 0 -or ($now-$heartbeat).TotalSeconds -gt 15 -or ($now-$health).TotalSeconds -lt 0 -or ($now-$health).TotalSeconds -gt 15) { throw "Baseline heartbeat/HEALTH is not fresh." }
    $record = Convert-SnapshotRecord $snapshot
    $state = New-BaseRecoveryState -ConfigHash $ConfigHash
    $state.accepted_incarnation_id = [string]$StatusObject.incarnation_id
    $state.primary_task_semantic_sha256 = [string]$TaskBinding.PrimarySemanticSha256
    $state.recovery_task_semantic_sha256 = [string]$TaskBinding.RecoverySemanticSha256
    $state.primary_task_security_sha256 = [string]$TaskBinding.PrimarySecuritySha256
    $state.recovery_task_security_sha256 = [string]$TaskBinding.RecoverySecuritySha256
    $state.last_healthy = [ordered]@{
        incarnation_id = [string]$StatusObject.incarnation_id
        started_at = $started.ToString("o")
        observed_at = $now.ToString("o")
        snapshot_sha256 = [string]$record.snapshot_sha256
        snapshot = $record
    }
    Save-RecoveryState $state
    $readback = Read-Json $RecoveryStatePath
    Assert-RecoveryState -State $readback -ConfigHash $ConfigHash
    Assert-RunningBaselineReady -State $readback -ConfigHash $ConfigHash | Out-Null
    return $readback
}

function Assert-RunningBaselineReady {
    param($State, [string]$ConfigHash)
    Assert-RecoveryState -State $State -ConfigHash $ConfigHash
    if ([string]$State.mode -cne "READY" -or $null -eq $State.last_healthy -or [string]::IsNullOrWhiteSpace([string]$State.accepted_incarnation_id)) { throw "RUNNING publication requires a validated READY last_healthy baseline." }
    if ([string]$State.accepted_incarnation_id -cne [string]$State.last_healthy.incarnation_id) { throw "RUNNING publication baseline incarnation binding mismatch." }
    if ([string]$State.primary_task_semantic_sha256 -notmatch '^[0-9a-f]{64}$' -or [string]$State.recovery_task_semantic_sha256 -notmatch '^[0-9a-f]{64}$' -or [string]$State.primary_task_security_sha256 -notmatch '^[0-9a-f]{64}$' -or [string]$State.recovery_task_security_sha256 -notmatch '^[0-9a-f]{64}$') { throw "RUNNING publication requires exact task semantic and security fingerprint baseline." }
    return $true
}

function Test-FreshHealthyStatus {
    param($StatusObject, [DateTimeOffset]$StartBoundary)
    if ($null -eq $StatusObject) { return $false }
    try {
        if (!(Test-JsonInteger $StatusObject.schema_version) -or [int]$StatusObject.schema_version -ne 2) { return $false }
        foreach($field in @("bridge","bridge_version","incarnation_id","heartbeat_incarnation_id","health_incarnation_id","health_status","started_at","heartbeat_at","health_at","health_since")){if($StatusObject.$field -isnot [string]){return $false}}
        if (!(Test-JsonInteger $StatusObject.process_pid) -or [int]$StatusObject.process_pid -le 0) { return $false }
        if ([string]$StatusObject.bridge -cne "instagramresearch-control-v1") { return $false }
        if ([string]$StatusObject.bridge_version -cne $ExpectedVer) { return $false }
        if (!(Test-Uuid $StatusObject.incarnation_id)) { return $false }
        if ([string]$StatusObject.heartbeat_incarnation_id -cne [string]$StatusObject.incarnation_id) { return $false }
        if ([string]$StatusObject.health_incarnation_id -cne [string]$StatusObject.incarnation_id) { return $false }
        if ([string]$StatusObject.health_status -cne "OK") { return $false }
        $started = ([DateTimeOffset]::Parse([string]$StatusObject.started_at)).ToUniversalTime()
        $heartbeat = ([DateTimeOffset]::Parse([string]$StatusObject.heartbeat_at)).ToUniversalTime()
        $health = ([DateTimeOffset]::Parse([string]$StatusObject.health_at)).ToUniversalTime()
        $healthSince = ([DateTimeOffset]::Parse([string]$StatusObject.health_since)).ToUniversalTime()
        if($heartbeat -lt $started -or $health -lt $started -or $healthSince -lt $started -or $healthSince -gt $health){return $false}
        $now = [DateTimeOffset]::UtcNow
        if ($started -le $StartBoundary -or $heartbeat -le $StartBoundary -or $health -le $StartBoundary) { return $false }
        if (($now - $heartbeat).TotalSeconds -lt 0 -or ($now - $heartbeat).TotalSeconds -gt 15) { return $false }
        if (($now - $health).TotalSeconds -lt 0 -or ($now - $health).TotalSeconds -gt 15) { return $false }
        $snapshot = Get-BridgeProcessSnapshot
        if (!$snapshot.IsValid) { return $false }
        try { Assert-PrimaryTaskRootAttestation -Snapshot $snapshot | Out-Null } catch { return $false }
        $statusProcessMatches = @($snapshot.Processes | Where-Object { [int]$_.ProcessId -eq [int]$StatusObject.process_pid })
        if ($statusProcessMatches.Count -ne 1) { return $false }
        if ([long]$snapshot.TaskRoot.CreationUtcTicks -le [long]$StartBoundary.ToUniversalTime().Ticks) { return $false }
        return $true
    }
    catch { return $false }
}


function Assert-TaskXmlAllowedChildren {
    param($Node, [string[]]$Allowed, [string]$Context)
    if ($null -eq $Node) { throw ($Context + " node missing.") }
    $allowedSet=New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::Ordinal)
    foreach($name in $Allowed){[void]$allowedSet.Add([string]$name)}
    foreach($child in @($Node.ChildNodes | Where-Object { $_.NodeType -eq [System.Xml.XmlNodeType]::Element })){
        if(!$allowedSet.Contains([string]$child.LocalName)){throw($Context+" contains unauthorized semantic element: "+[string]$child.LocalName)}
    }
}

function Assert-TaskXmlAllowedAttributes {
    param($Node, [string[]]$AllowedLocalNames, [string]$Context)
    if($null -eq $Node){throw($Context+" node missing.")}
    $allowed=New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::Ordinal);foreach($n in $AllowedLocalNames){[void]$allowed.Add([string]$n)}
    foreach($attr in @($Node.Attributes)){
        if([string]$attr.NamespaceURI -ceq "http://www.w3.org/2000/xmlns/"){continue}
        if(!$allowed.Contains([string]$attr.LocalName)){throw($Context+" contains unauthorized semantic attribute: "+[string]$attr.LocalName)}
    }
}

function Assert-TaskXmlClosedWorld {
    param([string]$TaskKind, [xml]$Xml, $Ns)
    if([string]$Xml.DocumentElement.LocalName -cne "Task" -or [string]$Xml.DocumentElement.NamespaceURI -cne "http://schemas.microsoft.com/windows/2004/02/mit/task"){throw($TaskKind+" Task root namespace/name drift.")}
    Assert-TaskXmlAllowedAttributes $Xml.DocumentElement @("version") ($TaskKind+" Task root")
    $taskVersion=[string]$Xml.DocumentElement.GetAttribute("version");if($taskVersion -notin @("1.3","1.4")){throw($TaskKind+" Task schema version outside reviewed range: "+$taskVersion)}
    foreach($element in @($Xml.SelectNodes("//*"))){
        if([string]$element.NamespaceURI -cne "http://schemas.microsoft.com/windows/2004/02/mit/task"){throw($TaskKind+" contains element outside reviewed Task Scheduler namespace: "+[string]$element.Name)}
        $allowedAttributes=switch([string]$element.LocalName){"Task"{@("version")};"Principal"{@("id")};"Actions"{@("Context")};default{@()}}
        foreach($attr in @($element.Attributes)){
            if([string]$attr.NamespaceURI -ceq "http://www.w3.org/2000/xmlns/"){continue}
            if(-not [string]::IsNullOrEmpty([string]$attr.NamespaceURI)){throw($TaskKind+" contains namespaced semantic attribute outside reviewed contract: "+[string]$attr.Name)}
        }
        Assert-TaskXmlAllowedAttributes $element $allowedAttributes ($TaskKind+" "+[string]$element.LocalName)
    }
    Assert-TaskXmlAllowedChildren $Xml.DocumentElement @("RegistrationInfo","Triggers","Principals","Settings","Actions") ($TaskKind+" Task root")
    $registration=@($Xml.SelectNodes("/t:Task/t:RegistrationInfo",$Ns)); if($registration.Count -ne 1){throw($TaskKind+" RegistrationInfo cardinality drift.")}
    Assert-TaskXmlAllowedAttributes $registration[0] @() ($TaskKind+" RegistrationInfo")
    Assert-TaskXmlAllowedChildren $registration[0] @("Description","URI") ($TaskKind+" RegistrationInfo")
    if(@($Xml.SelectNodes("/t:Task/t:RegistrationInfo/t:SecurityDescriptor",$Ns)).Count -ne 0){throw($TaskKind+" task must not embed a RegistrationInfo SecurityDescriptor.")}
    $expectedDescription=if($TaskKind -eq "PRIMARY"){"InstagramResearch fixed allowlisted request bridge"}else{"BACKLOG_051 bounded recovery check for InstagramResearchBridge only"}
    $expectedTaskName=if($TaskKind -eq "PRIMARY"){$TaskName}else{$RecoveryTaskName}
    $description=Get-TaskXmlNodeText $Xml $Ns "/t:Task/t:RegistrationInfo/t:Description"
    if($description -cne $expectedDescription){throw($TaskKind+" RegistrationInfo Description drift.")}
    $uri=Get-TaskXmlNodeText $Xml $Ns "/t:Task/t:RegistrationInfo/t:URI"
    if($uri -cne ("\"+$expectedTaskName)){throw($TaskKind+" RegistrationInfo URI drift.")}
    $principals=@($Xml.SelectNodes("/t:Task/t:Principals",$Ns)); if($principals.Count -ne 1){throw($TaskKind+" Principals container cardinality drift.")}
    Assert-TaskXmlAllowedAttributes $principals[0] @() ($TaskKind+" Principals")
    Assert-TaskXmlAllowedChildren $principals[0] @("Principal") ($TaskKind+" Principals")
    $principal=@($Xml.SelectNodes("/t:Task/t:Principals/t:Principal",$Ns)); if($principal.Count -ne 1){throw($TaskKind+" Principal cardinality drift.")}
    Assert-TaskXmlAllowedAttributes $principal[0] @("id") ($TaskKind+" Principal")
    Assert-TaskXmlAllowedChildren $principal[0] @("UserId","LogonType","RunLevel") ($TaskKind+" Principal")

    $actions=@($Xml.SelectNodes("/t:Task/t:Actions",$Ns)); if($actions.Count -ne 1){throw($TaskKind+" Actions container cardinality drift.")}
    Assert-TaskXmlAllowedAttributes $actions[0] @("Context") ($TaskKind+" Actions")
    Assert-TaskXmlAllowedChildren $actions[0] @("Exec") ($TaskKind+" Actions")
    $exec=@($Xml.SelectNodes("/t:Task/t:Actions/t:Exec",$Ns)); if($exec.Count -ne 1){throw($TaskKind+" Exec cardinality drift.")}
    Assert-TaskXmlAllowedAttributes $exec[0] @() ($TaskKind+" Exec")
    Assert-TaskXmlAllowedChildren $exec[0] @("Command","Arguments","WorkingDirectory") ($TaskKind+" Exec")

    $settings=@($Xml.SelectNodes("/t:Task/t:Settings",$Ns)); if($settings.Count -ne 1){throw($TaskKind+" Settings container cardinality drift.")}
    Assert-TaskXmlAllowedAttributes $settings[0] @() ($TaskKind+" Settings")
    Assert-TaskXmlAllowedChildren $settings[0] @(
        "MultipleInstancesPolicy","DisallowStartIfOnBatteries","StopIfGoingOnBatteries","AllowHardTerminate",
        "RunOnlyIfNetworkAvailable","AllowStartOnDemand","Enabled","Hidden","RunOnlyIfIdle","WakeToRun",
        "IdleSettings","UseUnifiedSchedulingEngine","StartWhenAvailable","ExecutionTimeLimit","Priority",
        "DeleteExpiredTaskAfter","DisallowStartOnRemoteAppSession","Volatile"
    ) ($TaskKind+" Settings")
    $idle=@($Xml.SelectNodes("/t:Task/t:Settings/t:IdleSettings",$Ns)); if($idle.Count -gt 1){throw($TaskKind+" IdleSettings cardinality drift.")}
    if($idle.Count -eq 1){Assert-TaskXmlAllowedChildren $idle[0] @("Duration","WaitTimeout","StopOnIdleEnd","RestartOnIdle") ($TaskKind+" IdleSettings")}

    $triggers=@($Xml.SelectNodes("/t:Task/t:Triggers",$Ns)); if($triggers.Count -ne 1){throw($TaskKind+" Triggers container cardinality drift.")}
    Assert-TaskXmlAllowedAttributes $triggers[0] @() ($TaskKind+" Triggers")
    if($TaskKind -eq "PRIMARY"){
        Assert-TaskXmlAllowedChildren $triggers[0] @("LogonTrigger") "PRIMARY Triggers"
        $trigger=@($Xml.SelectNodes("/t:Task/t:Triggers/t:LogonTrigger",$Ns)); if($trigger.Count -ne 1){throw "PRIMARY LogonTrigger cardinality drift."}
        Assert-TaskXmlAllowedAttributes $trigger[0] @() "PRIMARY LogonTrigger"
        Assert-TaskXmlAllowedChildren $trigger[0] @("UserId","Enabled") "PRIMARY LogonTrigger"
    } else {
        Assert-TaskXmlAllowedChildren $triggers[0] @("TimeTrigger") "RECOVERY Triggers"
        $trigger=@($Xml.SelectNodes("/t:Task/t:Triggers/t:TimeTrigger",$Ns)); if($trigger.Count -ne 1){throw "RECOVERY TimeTrigger cardinality drift."}
        Assert-TaskXmlAllowedAttributes $trigger[0] @() "RECOVERY TimeTrigger"
        Assert-TaskXmlAllowedChildren $trigger[0] @("StartBoundary","Enabled","Repetition") "RECOVERY TimeTrigger"
        $rep=@($Xml.SelectNodes("/t:Task/t:Triggers/t:TimeTrigger/t:Repetition",$Ns)); if($rep.Count -ne 1){throw "RECOVERY Repetition cardinality drift."}
        Assert-TaskXmlAllowedAttributes $rep[0] @() "RECOVERY Repetition"
        Assert-TaskXmlAllowedChildren $rep[0] @("Interval","StopAtDurationEnd") "RECOVERY Repetition"
    }
}

function Get-TaskXmlNodeText {
    param([xml]$Xml, $Ns, [string]$XPath, [bool]$Required=$true)
    $nodes=@($Xml.SelectNodes($XPath,$Ns))
    if($nodes.Count -gt 1){throw("Task XML has duplicate semantic node: "+$XPath)}
    if($nodes.Count -eq 0){if($Required){throw("Task XML missing semantic node: "+$XPath)};return $null}
    return [string]$nodes[0].InnerText
}

function Get-TaskSemanticFingerprint {
    param([xml]$Xml)
    $ns = New-Object System.Xml.XmlNamespaceManager($Xml.NameTable)
    $ns.AddNamespace("t", $Xml.DocumentElement.NamespaceURI)
    $parts = New-Object System.Collections.Generic.List[string]
    $parts.Add("version=" + [string]$Xml.DocumentElement.GetAttribute("version"))
    foreach ($path in @("/t:Task/t:RegistrationInfo", "/t:Task/t:Triggers", "/t:Task/t:Principals", "/t:Task/t:Settings", "/t:Task/t:Actions")) {
        $nodes = @($Xml.SelectNodes($path, $ns))
        if ($nodes.Count -ne 1) { throw ("Task XML semantic subtree cardinality drift: " + $path) }
        $parts.Add([string]$nodes[0].OuterXml)
    }
    return (Get-StringSha256 ($parts -join "`n"))
}

function Assert-TaskBooleanSetting {
    param([xml]$Xml, $Ns, [string]$XPath, [bool]$Expected, [string]$Label, [Nullable[bool]]$SchemaDefault=$null)
    $text = Get-TaskXmlNodeText $Xml $Ns $XPath $false
    if ($null -eq $text) {
        if ($null -eq $SchemaDefault -or [bool]$SchemaDefault -ne $Expected) { throw ($Label + " missing or omitted to a different schema default.") }
        return
    }
    $expectedText = if ($Expected) { "true" } else { "false" }
    if ($text -cne $expectedText) { throw ($Label + " drift.") }
}

function Assert-TaskXmlSemantics {
    param([string]$TaskKind, [xml]$Xml, [string]$ExpectedUser, [DateTimeOffset]$ExpectedRecoveryStartBoundary, $RegisteredTask=$null)
    $ns=New-Object System.Xml.XmlNamespaceManager($Xml.NameTable);$ns.AddNamespace("t",$Xml.DocumentElement.NamespaceURI)
    Assert-TaskXmlClosedWorld -TaskKind $TaskKind -Xml $Xml -Ns $ns
    $triggers=@($Xml.SelectNodes("/t:Task/t:Triggers/*",$ns)); if($triggers.Count -ne 1){throw($TaskKind+" task trigger count/type drift; found "+$triggers.Count)}
    $actions=@($Xml.SelectNodes("/t:Task/t:Actions/*",$ns)); if($actions.Count -ne 1 -or $actions[0].LocalName -ne "Exec"){throw($TaskKind+" task must contain exactly one Exec action.")}
    $principals=@($Xml.SelectNodes("/t:Task/t:Principals/t:Principal",$ns));if($principals.Count -ne 1){throw($TaskKind+" task principal cardinality drift.")}
    $principal=$principals[0]
    $principalId=[string]$principal.GetAttribute("id")
    if([string]::IsNullOrWhiteSpace($principalId)){throw($TaskKind+" task principal id missing.")}
    $actionsContainer=@($Xml.SelectNodes("/t:Task/t:Actions",$ns));if($actionsContainer.Count -ne 1 -or [string]$actionsContainer[0].GetAttribute("Context") -cne $principalId){throw($TaskKind+" task action/principal context binding drift.")}
    $user=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Principals/t:Principal/t:UserId"; if(!(Test-SameUser $user $ExpectedUser)){throw($TaskKind+" task principal user drift.")}
    $logon=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Principals/t:Principal/t:LogonType";if($logon -cne "InteractiveToken"){throw($TaskKind+" task logon type drift.")}
    $runLevel=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Principals/t:Principal/t:RunLevel" $false
    if($null -ne $runLevel){
        if($runLevel -cne "LeastPrivilege"){throw($TaskKind+" task run level drift.")}
    } else {
        if($null -eq $RegisteredTask -or $null -eq $RegisteredTask.Principal -or $null -eq $RegisteredTask.Principal.RunLevel){throw($TaskKind+" task run level omitted without independent registered-task read-back.")}
        $registeredRunLevel=[string]$RegisteredTask.Principal.RunLevel
        if($registeredRunLevel -notin @("Limited","LeastPrivilege","0")){throw($TaskKind+" task registered run level drift: "+$registeredRunLevel)}
    }
    if(@($Xml.SelectNodes("/t:Task/t:Principals/t:Principal/t:RequiredPrivileges",$ns)).Count -ne 0){throw($TaskKind+" task gained RequiredPrivileges.")}
    if(@($Xml.SelectNodes("/t:Task/t:Principals/t:Principal/t:GroupId",$ns)).Count -ne 0){throw($TaskKind+" task gained GroupId principal semantics.")}

    $expectedExe=$Pyw;$expectedArgs=if($TaskKind -eq "PRIMARY"){$TaskArguments}else{$RecoveryArguments}
    if((Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Actions/t:Exec/t:Command") -ine $expectedExe){throw($TaskKind+" action command drift.")}
    if((Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Actions/t:Exec/t:Arguments") -cne $expectedArgs){throw($TaskKind+" action arguments drift.")}
    if((Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Actions/t:Exec/t:WorkingDirectory") -ine $App){throw($TaskKind+" action working-directory drift.")}

    $multi=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Settings/t:MultipleInstancesPolicy" $false;if($null -ne $multi -and $multi -cne "IgnoreNew"){throw($TaskKind+" MultipleInstances drift.")}
    Assert-TaskBooleanSetting $Xml $ns "/t:Task/t:Settings/t:DisallowStartIfOnBatteries" $false ($TaskKind+" battery-start semantics") $true
    Assert-TaskBooleanSetting $Xml $ns "/t:Task/t:Settings/t:StopIfGoingOnBatteries" $false ($TaskKind+" battery-stop semantics") $true
    Assert-TaskBooleanSetting $Xml $ns "/t:Task/t:Settings/t:AllowHardTerminate" $true ($TaskKind+" AllowHardTerminate") $true
    Assert-TaskBooleanSetting $Xml $ns "/t:Task/t:Settings/t:RunOnlyIfNetworkAvailable" $false ($TaskKind+" RunOnlyIfNetworkAvailable") $false
    Assert-TaskBooleanSetting $Xml $ns "/t:Task/t:Settings/t:AllowStartOnDemand" $true ($TaskKind+" AllowStartOnDemand") $true
    Assert-TaskBooleanSetting $Xml $ns "/t:Task/t:Settings/t:Enabled" $true ($TaskKind+" Enabled") $true
    Assert-TaskBooleanSetting $Xml $ns "/t:Task/t:Settings/t:Hidden" $false ($TaskKind+" Hidden") $false
    Assert-TaskBooleanSetting $Xml $ns "/t:Task/t:Settings/t:RunOnlyIfIdle" $false ($TaskKind+" RunOnlyIfIdle") $false
    Assert-TaskBooleanSetting $Xml $ns "/t:Task/t:Settings/t:WakeToRun" $false ($TaskKind+" WakeToRun") $false
    Assert-TaskBooleanSetting $Xml $ns "/t:Task/t:Settings/t:IdleSettings/t:StopOnIdleEnd" $true ($TaskKind+" StopOnIdleEnd") $true
    Assert-TaskBooleanSetting $Xml $ns "/t:Task/t:Settings/t:IdleSettings/t:RestartOnIdle" $false ($TaskKind+" RestartOnIdle") $false
    $ues=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Settings/t:UseUnifiedSchedulingEngine";if($ues -cne "true"){throw($TaskKind+" UseUnifiedSchedulingEngine drift.")}
    $startAvailable=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Settings/t:StartWhenAvailable" $false; $expectedStartAvailable=if($TaskKind -eq "PRIMARY"){"true"}else{"false"};$actualStartAvailable=if($null -eq $startAvailable){"false"}else{$startAvailable};if($actualStartAvailable -cne $expectedStartAvailable){throw($TaskKind+" StartWhenAvailable drift.")}
    $execLimit=[System.Xml.XmlConvert]::ToTimeSpan((Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Settings/t:ExecutionTimeLimit"));$expectedLimit=if($TaskKind -eq "PRIMARY"){[TimeSpan]::Zero}else{[TimeSpan]::FromSeconds(55)};if($execLimit -ne $expectedLimit){throw($TaskKind+" ExecutionTimeLimit drift.")}
    $priorityText=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Settings/t:Priority" $false;$priority=if($null -eq $priorityText){7}else{[int]$priorityText};if($priority -ne 7){throw($TaskKind+" Priority drift.")}
    $deleteExpired=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Settings/t:DeleteExpiredTaskAfter" $false;if($null -ne $deleteExpired -and [System.Xml.XmlConvert]::ToTimeSpan($deleteExpired) -ne [TimeSpan]::Zero){throw($TaskKind+" DeleteExpiredTaskAfter drift.")}
    $remoteApp=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Settings/t:DisallowStartOnRemoteAppSession" $false;if($null -ne $remoteApp -and $remoteApp -cne "false"){throw($TaskKind+" DisallowStartOnRemoteAppSession drift.")}
    $volatile=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Settings/t:Volatile" $false;if($null -ne $volatile -and $volatile -cne "false"){throw($TaskKind+" Volatile drift.")}
    if(@($Xml.SelectNodes("/t:Task/t:Settings/t:MaintenanceSettings",$ns)).Count -ne 0){throw($TaskKind+" task gained MaintenanceSettings.")}
    if(@($Xml.SelectNodes("/t:Task/t:Settings/t:NetworkSettings",$ns)).Count -ne 0 -or @($Xml.SelectNodes("/t:Task/t:Settings/t:NetworkProfileName",$ns)).Count -ne 0){throw($TaskKind+" task gained network-profile constraints.")}

    if($TaskKind -eq "PRIMARY"){
        if($triggers[0].LocalName -ne "LogonTrigger"){throw "Primary trigger type drift."}
        $triggerUser=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Triggers/t:LogonTrigger/t:UserId";if(!(Test-SameUser $triggerUser $ExpectedUser)){throw "Primary LogonTrigger user drift."}
        $triggerEnabled=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Triggers/t:LogonTrigger/t:Enabled" $false;if($null -ne $triggerEnabled -and $triggerEnabled -cne "true"){throw "Primary LogonTrigger Enabled drift."}
        if(@($Xml.SelectNodes("/t:Task/t:Settings/t:RestartOnFailure",$ns)).Count -ne 0){throw "Primary task must not retain native RestartOnFailure semantics; recovery owns the single automatic start attempt."}
    } else {
        if($triggers[0].LocalName -ne "TimeTrigger"){throw "Recovery trigger type drift."}
        $triggerEnabled=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Triggers/t:TimeTrigger/t:Enabled" $false;if($null -ne $triggerEnabled -and $triggerEnabled -cne "true"){throw "Recovery trigger Enabled drift."}
        $repetition=@($Xml.SelectNodes("/t:Task/t:Triggers/t:TimeTrigger/t:Repetition",$ns));if($repetition.Count -ne 1){throw "Recovery repetition cardinality drift."}
        $interval=[System.Xml.XmlConvert]::ToTimeSpan((Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Triggers/t:TimeTrigger/t:Repetition/t:Interval"));if($interval -ne [TimeSpan]::FromMinutes(1)){throw "Recovery cadence drift."}
        if(@($Xml.SelectNodes("/t:Task/t:Triggers/t:TimeTrigger/t:Repetition/t:Duration",$ns)).Count -ne 0){throw "Recovery repetition Duration must remain omitted/indefinite."}
        $stopAtDuration=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Triggers/t:TimeTrigger/t:Repetition/t:StopAtDurationEnd";if($stopAtDuration -cne "true"){throw "Recovery StopAtDurationEnd drift."}
        if(@($Xml.SelectNodes("/t:Task/t:Settings/t:RestartOnFailure",$ns)).Count -ne 0){throw "Recovery task must not gain RestartOnFailure semantics."}
        $boundaryText=Get-TaskXmlNodeText $Xml $ns "/t:Task/t:Triggers/t:TimeTrigger/t:StartBoundary"
        $boundary=[DateTimeOffset]::Parse($boundaryText)
        if($null -ne $ExpectedRecoveryStartBoundary){if([Math]::Abs(($boundary-$ExpectedRecoveryStartBoundary).TotalSeconds) -gt 2){throw "Recovery first StartBoundary drifted from registered +60s boundary."}}
    }
}

function Assert-TaskDefinitionsReadback {
    param([string]$User, [DateTimeOffset]$ExpectedRecoveryStartBoundary)
    $primaryRegistered = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $recoveryRegistered = Get-ScheduledTask -TaskName $RecoveryTaskName -ErrorAction Stop
    [xml]$primaryXml = Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    [xml]$recoveryXml = Export-ScheduledTask -TaskName $RecoveryTaskName -ErrorAction Stop
    Assert-TaskXmlSemantics -TaskKind "PRIMARY" -Xml $primaryXml -ExpectedUser $User -RegisteredTask $primaryRegistered
    Assert-TaskXmlSemantics -TaskKind "RECOVERY" -Xml $recoveryXml -ExpectedUser $User -ExpectedRecoveryStartBoundary $ExpectedRecoveryStartBoundary -RegisteredTask $recoveryRegistered
    $primarySecurity=Get-TaskSecurityBinding -RegisteredTaskName $TaskName -ExpectedUser $User
    $recoverySecurity=Get-TaskSecurityBinding -RegisteredTaskName $RecoveryTaskName -ExpectedUser $User
    return [pscustomobject]@{
        PrimarySemanticSha256 = Get-TaskSemanticFingerprint $primaryXml
        RecoverySemanticSha256 = Get-TaskSemanticFingerprint $recoveryXml
        PrimarySecuritySha256 = [string]$primarySecurity.Sha256
        RecoverySecuritySha256 = [string]$recoverySecurity.Sha256
    }
}

function Enter-RecoverySerializationLock {
    param([int]$TimeoutSeconds=0)
    $parent = Split-Path $RecoveryLockPath
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try { return [System.IO.File]::Open($RecoveryLockPath,[System.IO.FileMode]::OpenOrCreate,[System.IO.FileAccess]::ReadWrite,[System.IO.FileShare]::None) }
        catch {
            if ($TimeoutSeconds -le 0 -or [DateTimeOffset]::UtcNow -ge $deadline) { throw }
            Start-Sleep -Milliseconds 250
        }
    } while ($true)
}


function Get-PreviousBoundSnapshotForMaintenance {
    if(!(Test-Path -LiteralPath $RecoveryStatePath -PathType Leaf)){return $null}
    $previous=Read-Json $RecoveryStatePath
    if($null -eq $previous){return $null}
    Assert-NoUnexpectedProperties $previous @(
        "schema_version","config_sha256","mode","episode_id","episode_reason","failure_boundary","deadline_anchor_at","deadline_anchor_source",
        "deadline_anchor_evidence","acceptance_deadline_at","start_attempted","start_attempt_at","old_incarnation_id","old_snapshot_sha256","old_snapshot",
        "accepted_incarnation_id","primary_task_semantic_sha256","recovery_task_semantic_sha256","primary_task_security_sha256","recovery_task_security_sha256",
        "health_missing_incarnation_id","health_missing_since","last_healthy","last_successful_episode_id","failure_detail","updated_at","state_sha256"
    ) "Previous recovery state"
    if(!(Test-JsonInteger $previous.schema_version)){throw "Previous recovery-state schema type is invalid."}
    $schema=[int]$previous.schema_version
    if($schema -notin @(4,5)){throw("Previous recovery-state schema is not a reviewed upgrade source: "+$schema)}
    if($null -eq $previous.last_healthy){return $null}
    Assert-NoUnexpectedProperties $previous.last_healthy @("incarnation_id","started_at","observed_at","snapshot_sha256","snapshot") "Previous last_healthy"
    if([string]$previous.last_healthy.snapshot_sha256 -notmatch '^[0-9a-f]{64}$'){throw "Previous last_healthy snapshot hash invalid."}
    Assert-SnapshotRecordIntegrity -Record $previous.last_healthy.snapshot
    if([string]$previous.last_healthy.snapshot.snapshot_sha256 -cne [string]$previous.last_healthy.snapshot_sha256){throw "Previous last_healthy snapshot binding mismatch."}
    return $previous.last_healthy.snapshot
}

function Assert-UninstallReadback {
    param($OldSnapshotRecord)
    $recovery = Get-ScheduledTask -TaskName $RecoveryTaskName -ErrorAction SilentlyContinue
    $primary = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $recovery) { throw "Uninstall read-back found recovery task residue." }
    if ($null -ne $primary) { throw "Uninstall read-back found primary task residue." }
    $expectedAction = Get-ExpectedBridgeTaskActionIdentity
    if (@(Get-BridgeProcesses -TaskAction $expectedAction).Count -ne 0) { throw "Uninstall read-back found a live bridge task-root/process tree." }
    if ($null -ne $OldSnapshotRecord -and !(Test-OldTreeAbsent $OldSnapshotRecord)) { throw "Uninstall read-back found a previously bound bridge process identity still alive." }
    return $true
}

function Invoke-Uninstall {
    $config = Assert-RecoveryInputs
    $configHash = Get-FileSha256 $RecoveryConfig
    $lock = $null
    $success = $false
    $oldSnapshot = $null
    try {
        $lock = Enter-RecoverySerializationLock -TimeoutSeconds 65
        Write-DesiredState -Desired "STOPPED" -ConfigHash $configHash
        $desiredReadback = Read-Json $DesiredStatePath
        if ($null -eq $desiredReadback -or [string]$desiredReadback.desired_state -cne "STOPPED" -or [string]$desiredReadback.config_sha256 -cne $configHash) { throw "Uninstall STOPPED desired-state read-back failed." }

        $recoveryTask = Get-ScheduledTask -TaskName $RecoveryTaskName -ErrorAction SilentlyContinue
        if ($null -ne $recoveryTask -and $recoveryTask.State.ToString() -eq "Running") { Stop-ScheduledTask -TaskName $RecoveryTaskName -ErrorAction Stop }

        $primaryTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -ne $primaryTask) {
            $action = Get-BridgeTaskActionIdentity
            $procs = @(Get-BridgeProcesses -TaskAction $action)
            if ($procs.Count -gt 0) {
                $snapshot = New-BridgeProcessSnapshot -Processes $procs -TaskAction $action
                if (!$snapshot.IsValid) { throw ("Uninstall refuses ambiguous bridge lineage: " + (@($snapshot.FailureReasons)-join ",")) }
                $kill = Invoke-ValidatedBridgeTreeKill -ExpectedSnapshot $snapshot
                $oldSnapshot = $kill.Snapshot
                $deadline = [DateTimeOffset]::UtcNow.AddSeconds(5)
                while ([DateTimeOffset]::UtcNow -lt $deadline -and !(Test-OldTreeAbsent $oldSnapshot)) { Start-Sleep -Milliseconds 250 }
                if (!(Test-OldTreeAbsent $oldSnapshot)) { throw "Uninstall could not prove killed bridge lineage absent." }
            }
            $currentPrimary = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
            if ($currentPrimary.State.ToString() -eq "Running") { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop }
        }

        # A previous last_healthy record also guards against orphan residue whose task-root vanished.
        $stateBeforeRemoval = Read-Json $RecoveryStatePath
        if ($null -ne $stateBeforeRemoval) {
            Assert-RecoveryState -State $stateBeforeRemoval -ConfigHash $configHash
            if ($null -ne $stateBeforeRemoval.last_healthy -and !(Test-OldTreeAbsent $stateBeforeRemoval.last_healthy.snapshot)) { throw "Uninstall found last_healthy process residue outside current root." }
        }

        if ($null -ne (Get-ScheduledTask -TaskName $RecoveryTaskName -ErrorAction SilentlyContinue)) { Unregister-ScheduledTask -TaskName $RecoveryTaskName -Confirm:$false -ErrorAction Stop }
        if ($null -ne (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop }
        Assert-UninstallReadback -OldSnapshotRecord $oldSnapshot | Out-Null

        if (Test-Path -LiteralPath $DesiredStatePath) { Remove-Item -LiteralPath $DesiredStatePath -Force -ErrorAction Stop }
        if (Test-Path -LiteralPath $RecoveryStatePath) { Remove-Item -LiteralPath $RecoveryStatePath -Force -ErrorAction Stop }
        if (Test-Path -LiteralPath $LocalRecoveryDir) {
            $left = @(Get-ChildItem -LiteralPath $LocalRecoveryDir -Force -ErrorAction Stop)
            if ($left.Count -eq 0) { Remove-Item -LiteralPath $LocalRecoveryDir -Force -ErrorAction Stop }
        }
        $success = $true
    }
    finally {
        if ($null -ne $lock) { $lock.Dispose() }
    }
    if (!$success) { throw "Uninstall did not complete verified cleanup." }
    if (Test-Path -LiteralPath $RecoveryLockPath) { Remove-Item -LiteralPath $RecoveryLockPath -Force -ErrorAction Stop }
    Log "PASS uninstall verified: recovery task absent, primary task absent, relevant bridge lineage absent"
}

function Write-HealthRequest {
    $now = [DateTimeOffset]::UtcNow
    $rid = "installer-" + $now.ToString("yyyyMMddTHHmmssfffZ") + "-health"

    $obj = [ordered]@{
        schema_version = 1
        bridge         = "instagramresearch-control-v1"
        state          = "PENDING"
        request_id     = $rid
        action         = "HEALTH"
        issued_by      = "AVANZA_MCP"
        issued_at      = $now.ToString("o")
        expires_at     = $now.AddMinutes(30).ToString("o")
        reason         = "Bridge scheduled-task live acceptance"
    }

    Write-JsonAtomic -Path $Request -Object $obj

    return $rid
}

# Dot-source only for executable tests; -File execution continues into the selected mode.
if ($MyInvocation.InvocationName -eq ".") { return }

if ($Mode -eq "Uninstall") {
    Invoke-Uninstall
    exit 0
}

Log "Install/upgrade start"

if (!(Test-Path -LiteralPath $Bridge -PathType Leaf)) { throw "Missing bridge source file: $Bridge" }
if (!(Test-Path -LiteralPath $Py -PathType Leaf)) { throw "Missing reviewed venv python.exe: $Py" }
if (!(Test-Path -LiteralPath $Pyw -PathType Leaf)) { throw "Missing reviewed venv pythonw.exe: $Pyw" }
if (!(Test-Path -LiteralPath $TaskKill -PathType Leaf)) { throw "Missing Windows taskkill.exe: $TaskKill" }
if (!(Test-Path -LiteralPath $PowerShellExe -PathType Leaf)) { throw "Missing reviewed Windows PowerShell executable: $PowerShellExe" }

$config = Assert-RecoveryInputs
$configHash = Get-FileSha256 $RecoveryConfig
$maintenanceLock = $null
try {
    # Same mutex as recovery owns STOPPED/MAINTENANCE and kill/start authority.
    $maintenanceLock = Enter-RecoverySerializationLock -TimeoutSeconds 65
    Write-DesiredState -Desired "STOPPED" -ConfigHash $configHash
    $previousBoundSnapshot=Get-PreviousBoundSnapshotForMaintenance
    Log "Recovery candidate hashes verified; serialized desired_state=STOPPED and captured any previous bound lineage before task maintenance"

    $CompileTargets = @(
        $Bridge,
        (Join-Path $App "creator_registry.py"),
        (Join-Path $App "creator_registration.py"),
        (Join-Path $App "influencer_evaluation.py"),
        (Join-Path $App "creator_monitor.py"),
        (Join-Path $App "creator_recent_check.py"),
        (Join-Path $App "youtube_creator_evaluation.py"),
        (Join-Path $App "tiktok_camofox_sync.py")
    )
    foreach ($target in $CompileTargets) {
        if (!(Test-Path $target)) { throw ("Missing runtime target: " + $target) }
        & $Py -m py_compile $target
        if ($LASTEXITCODE -ne 0) { throw ((Split-Path $target -Leaf) + " failed py_compile") }
    }
    Log "py_compile OK for bridge + fixed creator adapters"

    # While serialized and STOPPED, clean current exact tree before unregistering its task definition.
    $existingRecovery = Get-ScheduledTask -TaskName $RecoveryTaskName -ErrorAction SilentlyContinue
    if ($existingRecovery -and $existingRecovery.State.ToString() -eq "Running") { Stop-ScheduledTask -TaskName $RecoveryTaskName -ErrorAction Stop }
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        $null = Stop-BridgeProcesses
        $existingNow = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        if ($existingNow.State.ToString() -eq "Running") { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    }
    if ($existingRecovery) { Unregister-ScheduledTask -TaskName $RecoveryTaskName -Confirm:$false -ErrorAction Stop }

    # Do not discard previous lineage evidence until maintenance proves every bound identity absent.
    if ($null -ne $previousBoundSnapshot -and !(Test-OldTreeAbsent $previousBoundSnapshot)) { throw "Install/upgrade found previous last_healthy process residue; refusing to replace recovery state." }
    $unattested=@(Get-BridgeProcesses -TaskAction (Get-ExpectedBridgeTaskActionIdentity))
    if($unattested.Count -ne 0){throw "Install/upgrade found an exact bridge action process after primary task removal; refusing duplicate/unattested runtime."}

    # Bounded v0.5.0 -> v0.6.0 persisted-state compatibility. The bridge helper uses
    # the same strict JSON parser/state validator as runtime and atomically captures an
    # exact rollback backup. Runtime accepts v0.5.0 only when those exact bytes match
    # that backup, then the normal atomic state write changes only bridge_version +
    # updated_at while preserving paused, processed_request_ids and last_* exactly.
    $stateUpgradePreparation = @(& $Py $Bridge --root $Root --prepare-state-upgrade 2>&1)
    if ($LASTEXITCODE -ne 0) { throw ("Persisted bridge-state upgrade preparation failed: " + ($stateUpgradePreparation -join " | ")) }
    Log ("Persisted bridge-state upgrade preparation PASS " + ($stateUpgradePreparation -join " "))

    Initialize-RecoveryState -ConfigHash $configHash | Out-Null

    $bridgeRuntimeLock = Join-Path $env:LOCALAPPDATA "InstagramResearch\research_request_bridge.lock"
    if (Test-Path -LiteralPath $bridgeRuntimeLock) { Remove-Item -LiteralPath $bridgeRuntimeLock -Force -ErrorAction Stop }

    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $Pyw -Argument $TaskArguments -WorkingDirectory $App
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "InstagramResearch fixed allowlisted request bridge"
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force -ErrorAction Stop | Out-Null

    $recoveryAction = New-ScheduledTaskAction -Execute $Pyw -Argument $RecoveryArguments -WorkingDirectory $App
    $recoveryTriggerAt = [DateTimeOffset]::Now.AddSeconds(60)
    $recoveryTrigger = New-ScheduledTaskTrigger -Once -At $recoveryTriggerAt.LocalDateTime -RepetitionInterval ([TimeSpan]::FromMinutes(1))
    $recoverySettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::FromSeconds(55))
    $recoveryTask = New-ScheduledTask -Action $recoveryAction -Trigger $recoveryTrigger -Principal $principal -Settings $recoverySettings -Description "BACKLOG_051 bounded recovery check for InstagramResearchBridge only"
    Register-ScheduledTask -TaskName $RecoveryTaskName -InputObject $recoveryTask -Force -ErrorAction Stop | Out-Null

    $taskBinding = Assert-TaskDefinitionsReadback -User $user -ExpectedRecoveryStartBoundary $recoveryTriggerAt
    Log ("Exact primary+recovery task semantic/security read-back PASS primary_semantic_sha256=" + $taskBinding.PrimarySemanticSha256 + " recovery_semantic_sha256=" + $taskBinding.RecoverySemanticSha256 + " primary_security_sha256=" + $taskBinding.PrimarySecuritySha256 + " recovery_security_sha256=" + $taskBinding.RecoverySecuritySha256)

    # Install-only live baseline; desired_state remains STOPPED until state is seeded and read back.
    $primaryStartBoundary = [DateTimeOffset]::UtcNow
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Wait-For {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (!$t -or $t.State.ToString() -ne "Running") { return $false }
        return (Test-FreshHealthyStatus -StatusObject (Read-Json $Status) -StartBoundary $primaryStartBoundary)
    } 30 "Bridge did not expose a strict fresh v0.6.0 incarnation/heartbeat/HEALTH baseline."

    $baseline = Read-Json $Status
    Log ("Primary strict baseline OK incarnation=" + [string]$baseline.incarnation_id + " started_at=" + [string]$baseline.started_at)
    $healthId = Write-HealthRequest
    Wait-For {
        $st = Read-Json $BridgeState
        if (!$st) { return $false }
        return ($st.last_request_id -eq $healthId -and $st.last_action -eq "HEALTH" -and $st.last_result -eq "OK")
    } 30 "HEALTH request was not processed successfully."
    Wait-For {
        $s2 = Read-Json $Status
        if (!$s2 -or [string]$s2.incarnation_id -cne [string]$baseline.incarnation_id) { return $false }
        return (Test-FreshHealthyStatus -StatusObject $s2 -StartBoundary $primaryStartBoundary)
    } 30 "Status contract did not expose fresh successful business HEALTH and recovery HEALTH state."

    $finalStatus = Read-Json $Status
    $seeded = Seed-RecoveryHealthyBaseline -ConfigHash $configHash -StatusObject $finalStatus -TaskBinding $taskBinding
    Assert-RunningBaselineReady -State $seeded -ConfigHash $configHash | Out-Null
    $taskBindingFinal = Assert-TaskDefinitionsReadback -User $user -ExpectedRecoveryStartBoundary $recoveryTriggerAt
    if ([string]$taskBindingFinal.PrimarySemanticSha256 -cne [string]$seeded.primary_task_semantic_sha256 -or [string]$taskBindingFinal.RecoverySemanticSha256 -cne [string]$seeded.recovery_task_semantic_sha256 -or [string]$taskBindingFinal.PrimarySecuritySha256 -cne [string]$seeded.primary_task_security_sha256 -or [string]$taskBindingFinal.RecoverySecuritySha256 -cne [string]$seeded.recovery_task_security_sha256) { throw "Task semantic/security fingerprint drift before RUNNING publication." }

    # RUNNING becomes observable only after same-lock state seed + exact task read-back.
    Write-DesiredState -Desired "RUNNING" -ConfigHash $configHash
    $desiredFinal = Read-Json $DesiredStatePath
    if ($null -eq $desiredFinal -or [string]$desiredFinal.desired_state -cne "RUNNING" -or [string]$desiredFinal.config_sha256 -cne $configHash) { throw "RUNNING desired-state publication read-back failed." }

    # Keep the exact v0.5.0 state backup after install-only success. The external
    # bounded upgrade/finalization harness owns the wider acceptance boundary and may
    # restore this backup on any later failure. Only after that wider gate passes may
    # it invoke --commit-state-upgrade to delete the rollback backup.
    Log ("PASS install-only validation; primary=" + $TaskName + " recovery=" + $RecoveryTaskName + " seeded_last_healthy=" + [string]$seeded.accepted_incarnation_id + " desired_state=RUNNING state_upgrade_backup_retained_for_external_commit=" + [string](Test-Path -LiteralPath $BridgeStateUpgradeBackup -PathType Leaf) + " transaction_retained=" + [string](Test-Path -LiteralPath $BridgeStateUpgradeTransaction -PathType Leaf))
}
catch {
    $installFailure = $_
    Log ("Install/upgrade fail-closed: " + $installFailure.Exception.GetType().FullName + ": " + $installFailure.Exception.Message)
    $hasUpgradeBackup = Test-Path -LiteralPath $BridgeStateUpgradeBackup -PathType Leaf
    $hasUpgradeTransaction = Test-Path -LiteralPath $BridgeStateUpgradeTransaction -PathType Leaf
    if ($hasUpgradeBackup -or $hasUpgradeTransaction) {
        try {
            # Candidate-side rollback is permitted only after STOPPED intent and exact
            # writer/task quiescence are both proven under the same maintenance lock.
            # Do not suppress a stop/lineage/absence failure and never restore state
            # while a v0.6.0 writer may still be live or restartable.
            Assert-InstallRollbackWriterQuiescence -ConfigHash $configHash
            $stateUpgradeRestore = @(& $Py $Bridge --root $Root --restore-state-upgrade-backup 2>&1)
            if ($LASTEXITCODE -ne 0) { throw ("Persisted bridge-state rollback failed: " + ($stateUpgradeRestore -join " | ")) }
            Log ("Persisted bridge-state rollback PASS " + ($stateUpgradeRestore -join " "))
        }
        catch {
            throw ("Install/upgrade failed and persisted bridge-state rollback was not proven. Original=" + $installFailure.Exception.Message + " rollback=" + $_.Exception.Message)
        }
    }
    throw $installFailure
}
finally {
    if ($null -ne $maintenanceLock) { $maintenanceLock.Dispose() }
}

Write-Host ""
Write-Host "BACKLOG_051 revised recovery candidate installation completed."
Write-Host "This does not constitute RRRA_V1 runtime acceptance."
