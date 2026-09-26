param(
    [ValidateSet("Plan", "Apply", "Verify")]
    [string]$Action = "Plan"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $env:LOCALAPPDATA) {
    throw "LOCALAPPDATA is required."
}

$OldRoot = Join-Path $env:LOCALAPPDATA "InstagramResearch"
$NewRoot = Join-Path $env:LOCALAPPDATA "InfluencerResearch"
$ArchiveRoot = Join-Path $NewRoot "legacy-archive-2026-09-26"

$ActiveNames = @(
    "secrets",
    "chrome-profile",
    "auth-venv",
    "camofox-poc",
    "tiktok_runtime_v1.lock"
)

$ObsoleteNames = @(
    "venv",
    "recovery",
    "task-backup-20260917",
    "bridge_watchdog.py",
    "bridge_watchdog.log",
    "recovery.guard.lock",
    "research_request_bridge.lock"
)

function Assert-LegacyBridgeStopped {
    foreach ($name in @("InstagramResearchBridge", "InstagramResearchBridgeRecovery")) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            throw "Legacy Scheduled Task still exists: $name"
        }
    }

    $legacy = @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.CommandLine -match "research_request_bridge\.py" -and
                $_.CommandLine -match "InstagramResearch"
            }
    )
    if ($legacy.Count -gt 0) {
        throw "Legacy research_request_bridge.py process is still running."
    }
}

function Show-Plan {
    Write-Host "Old root: $OldRoot"
    Write-Host "New root: $NewRoot"

    if (-not (Test-Path -LiteralPath $OldRoot -PathType Container)) {
        Write-Host "Legacy root is already absent."
        return
    }

    Write-Host ""
    Write-Host "Move to active InfluencerResearch namespace:"
    foreach ($name in $ActiveNames) {
        $source = Join-Path $OldRoot $name
        if (Test-Path -LiteralPath $source) {
            Write-Host "  $name"
        }
    }

    Write-Host ""
    Write-Host "Delete retired bridge-only artifacts:"
    foreach ($name in $ObsoleteNames) {
        $source = Join-Path $OldRoot $name
        if (Test-Path -LiteralPath $source) {
            Write-Host "  $name"
        }
    }

    $known = @($ActiveNames + $ObsoleteNames)
    $remaining = @(
        Get-ChildItem -LiteralPath $OldRoot -Force |
            Where-Object { $_.Name -notin $known }
    )
    if ($remaining.Count -gt 0) {
        Write-Host ""
        Write-Host "Preserve remaining historical material under:"
        Write-Host "  $ArchiveRoot"
        foreach ($item in $remaining) {
            Write-Host "  $($item.Name)"
        }
    }
}

function Move-ActiveItem([string]$Name) {
    $source = Join-Path $OldRoot $Name
    if (-not (Test-Path -LiteralPath $source)) {
        return
    }

    $destination = Join-Path $NewRoot $Name
    if (Test-Path -LiteralPath $destination) {
        throw "Refusing to overwrite existing destination: $destination"
    }

    Write-Host "Moving active state: $Name"
    Move-Item -LiteralPath $source -Destination $destination
}

function Remove-ObsoleteItem([string]$Name) {
    $source = Join-Path $OldRoot $Name
    if (-not (Test-Path -LiteralPath $source)) {
        return
    }

    Write-Host "Removing retired bridge artifact: $Name"
    Remove-Item -LiteralPath $source -Recurse -Force
}

function Archive-Remaining {
    $remaining = @(Get-ChildItem -LiteralPath $OldRoot -Force)
    if ($remaining.Count -eq 0) {
        return
    }

    New-Item -ItemType Directory -Force -Path $ArchiveRoot | Out-Null
    foreach ($item in $remaining) {
        $destination = Join-Path $ArchiveRoot $item.Name
        if (Test-Path -LiteralPath $destination) {
            throw "Refusing to overwrite existing archive item: $destination"
        }
        Write-Host "Archiving historical item: $($item.Name)"
        Move-Item -LiteralPath $item.FullName -Destination $destination
    }
}

function Verify-Migration {
    Assert-LegacyBridgeStopped

    if (Test-Path -LiteralPath $OldRoot) {
        $left = @(Get-ChildItem -LiteralPath $OldRoot -Force)
        if ($left.Count -gt 0) {
            throw "Legacy root still contains $($left.Count) item(s): $OldRoot"
        }
        throw "Legacy root still exists: $OldRoot"
    }

    foreach ($required in @("docker-runtime.json")) {
        $path = Join-Path $NewRoot $required
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Expected current runtime file is missing: $path"
        }
    }

    Write-Host "LOCAL_NAMESPACE_MIGRATION_OK"
    Write-Host "Canonical host runtime: $NewRoot"
}

switch ($Action) {
    "Plan" {
        Show-Plan
    }
    "Apply" {
        Assert-LegacyBridgeStopped
        New-Item -ItemType Directory -Force -Path $NewRoot | Out-Null

        foreach ($name in $ActiveNames) {
            Move-ActiveItem $name
        }
        foreach ($name in $ObsoleteNames) {
            Remove-ObsoleteItem $name
        }

        Archive-Remaining

        if (Test-Path -LiteralPath $OldRoot) {
            $left = @(Get-ChildItem -LiteralPath $OldRoot -Force)
            if ($left.Count -ne 0) {
                throw "Legacy root is not empty after migration."
            }
            Remove-Item -LiteralPath $OldRoot -Force
        }

        Verify-Migration
    }
    "Verify" {
        Verify-Migration
    }
}
