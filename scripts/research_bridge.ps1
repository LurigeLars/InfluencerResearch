param(
    [ValidateSet("Install", "Uninstall", "Status")]
    [string]$Mode = "Install"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA is required." }

$TaskName = "InstagramResearchBridge"
$LegacyRecoveryTaskName = "InstagramResearchBridgeRecovery"
$App = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Root = (Resolve-Path (Join-Path $App "..")).Path
$Bridge = Join-Path $App "research_request_bridge.py"
$StatePath = Join-Path $Root "state\research_bridge_status.json"
$Py = Join-Path $env:LOCALAPPDATA "InstagramResearch\venv\Scripts\python.exe"
$Pyw = Join-Path $env:LOCALAPPDATA "InstagramResearch\venv\Scripts\pythonw.exe"

function Get-ExpectedBridgeVersion {
    $text = Get-Content -LiteralPath $Bridge -Raw
    $match = [regex]::Match($text, '(?m)^BRIDGE_VERSION\s*=\s*"([^"]+)"')
    if (-not $match.Success) { throw "Unable to read BRIDGE_VERSION from $Bridge" }
    return $match.Groups[1].Value
}

function Remove-BridgeTask {
    param([Parameter(Mandatory=$true)][string]$Name)

    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -eq $task) { return }

    if ($task.State.ToString() -eq "Running") {
        Stop-ScheduledTask -TaskName $Name -ErrorAction Stop
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(15)
        do {
            Start-Sleep -Milliseconds 250
            $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
            if ($null -eq $task -or $task.State.ToString() -ne "Running") { break }
        } while ([DateTimeOffset]::UtcNow -lt $deadline)

        if ($null -ne $task -and $task.State.ToString() -eq "Running") {
            throw "Scheduled task did not stop cleanly: $Name"
        }
    }

    if ($null -ne (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue)) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction Stop
    }
}

function Read-BridgeStatus {
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json }
    catch { return $null }
}

function Get-PropertyValue {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Test-FreshHealthyBridge {
    param(
        [Parameter(Mandatory=$true)][string]$ExpectedVersion,
        [Parameter(Mandatory=$true)][DateTimeOffset]$StartBoundary
    )

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task -or $task.State.ToString() -ne "Running") { return $false }

    $status = Read-BridgeStatus
    if ($null -eq $status) { return $false }
    if ([string](Get-PropertyValue $status "bridge") -cne "instagramresearch-control-v1") { return $false }
    if ([string](Get-PropertyValue $status "bridge_version") -cne $ExpectedVersion) { return $false }
    if ([string](Get-PropertyValue $status "health_status") -cne "OK") { return $false }
    if ([string]::IsNullOrWhiteSpace([string](Get-PropertyValue $status "incarnation_id"))) { return $false }

    try {
        $started = [DateTimeOffset]::Parse([string](Get-PropertyValue $status "started_at")).ToUniversalTime()
        $heartbeat = [DateTimeOffset]::Parse([string](Get-PropertyValue $status "heartbeat_at")).ToUniversalTime()
    }
    catch { return $false }

    if ($started -lt $StartBoundary.AddSeconds(-2)) { return $false }
    $age = ([DateTimeOffset]::UtcNow - $heartbeat).TotalSeconds
    if ($age -lt -5 -or $age -gt 20) { return $false }
    return $true
}

function Show-Status {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $status = Read-BridgeStatus
    [pscustomobject]@{
        task_name = $TaskName
        task_present = ($null -ne $task)
        task_state = if ($null -ne $task) { $task.State.ToString() } else { "ABSENT" }
        bridge_status = $status
    } | ConvertTo-Json -Depth 8
}

if ($Mode -eq "Status") {
    Show-Status
    exit 0
}

if ($Mode -eq "Uninstall") {
    Remove-BridgeTask -Name $LegacyRecoveryTaskName
    Remove-BridgeTask -Name $TaskName
    Write-Host "RESEARCH_BRIDGE_UNINSTALLED"
    exit 0
}

if (-not (Test-Path -LiteralPath $Bridge -PathType Leaf)) { throw "Missing bridge source: $Bridge" }
if (-not (Test-Path -LiteralPath $Py -PathType Leaf)) { throw "Missing reviewed Python runtime: $Py" }
if (-not (Test-Path -LiteralPath $Pyw -PathType Leaf)) { throw "Missing reviewed Python windowless runtime: $Pyw" }

$expectedVersion = Get-ExpectedBridgeVersion
$compileTargets = @(
    "research_request_bridge.py",
    "creator_registry.py",
    "creator_registration.py",
    "influencer_evaluation.py",
    "creator_monitor.py",
    "creator_recent_check.py",
    "youtube_creator_evaluation.py",
    "tiktok_camofox_sync.py"
)

foreach ($name in $compileTargets) {
    $target = Join-Path $App $name
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "Missing bridge runtime target: $target" }
    & $Py -m py_compile $target
    if ($LASTEXITCODE -ne 0) { throw "py_compile failed: $name" }
}

# Clean only scheduled tasks owned by this bridge. The runtime itself enforces single-instance.
Remove-BridgeTask -Name $LegacyRecoveryTaskName
Remove-BridgeTask -Name $TaskName

$upgradePrepared = $false
$taskRegistered = $false
try {
    $prepareOutput = @(& $Py $Bridge --root $Root --prepare-state-upgrade 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "Bridge state preparation failed: $($prepareOutput -join ' | ')" }
    $upgradePrepared = $true

    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $arguments = '"' + $Bridge + '" --root "' + $Root + '"'
    $action = New-ScheduledTaskAction -Execute $Pyw -Argument $arguments -WorkingDirectory $App
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settingsParams = @{
        MultipleInstances = "IgnoreNew"
        StartWhenAvailable = $true
        AllowStartIfOnBatteries = $true
        DontStopIfGoingOnBatteries = $true
        ExecutionTimeLimit = [TimeSpan]::Zero
    }
    $settings = New-ScheduledTaskSettingsSet @settingsParams
    $taskParams = @{
        Action = $action
        Trigger = $trigger
        Principal = $principal
        Settings = $settings
        Description = "InfluencerResearch fixed allowlisted request bridge"
    }
    $task = New-ScheduledTask @taskParams

    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force -ErrorAction Stop | Out-Null
    $taskRegistered = $true

    $startBoundary = [DateTimeOffset]::UtcNow
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
    do {
        if (Test-FreshHealthyBridge -ExpectedVersion $expectedVersion -StartBoundary $startBoundary) { break }
        Start-Sleep -Milliseconds 500
    } while ([DateTimeOffset]::UtcNow -lt $deadline)

    if (-not (Test-FreshHealthyBridge -ExpectedVersion $expectedVersion -StartBoundary $startBoundary)) {
        throw "Bridge did not reach a fresh healthy state."
    }

    $commitOutput = @(& $Py $Bridge --root $Root --commit-state-upgrade 2>&1)
    if ($LASTEXITCODE -ne 0) { throw "Bridge state upgrade commit failed: $($commitOutput -join ' | ')" }

    Write-Host "RESEARCH_BRIDGE_READY version=$expectedVersion"
    Show-Status
}
catch {
    $failure = $_
    if ($taskRegistered) { try { Remove-BridgeTask -Name $TaskName } catch {} }
    if ($upgradePrepared) {
        try { & $Py $Bridge --root $Root --restore-state-upgrade-backup *> $null } catch {}
    }
    throw $failure
}
