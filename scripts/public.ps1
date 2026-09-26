param(
    [ValidateSet("Up", "Down", "Status", "Logs")]
    [string]$Action = "Status"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI is not available on PATH."
}

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Compose = Join-Path $Repo "compose.public.yaml"
$GatewayEnv = Join-Path $Repo "public\gateway.env"
$RuntimeNetwork = "influencerresearch_runtime"

function PublicCompose([string[]]$ComposeArgs) {
    & docker compose -f $Compose @ComposeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose public stack failed with exit code $LASTEXITCODE"
    }
}

function Require-LocalConfig {
    if (-not (Test-Path -LiteralPath $GatewayEnv -PathType Leaf)) {
        throw "Missing local gateway config. Create the gitignored deployment-local file out of band."
    }

    & docker network inspect $RuntimeNetwork *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker network $RuntimeNetwork is missing. Start the base runtime first: scripts\runtime.ps1 -Action Up"
    }
}

switch ($Action) {
    "Up" {
        Require-LocalConfig
        PublicCompose -ComposeArgs @("up", "-d")

        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
        do {
            $health = (& docker inspect -f "{{.State.Health.Status}}" influencerresearch-gateway 2>$null).Trim()
            if ($health -eq "healthy") { break }
            Start-Sleep -Milliseconds 500
        } while ([DateTimeOffset]::UtcNow -lt $deadline)

        if ($health -ne "healthy") {
            PublicCompose -ComposeArgs @("logs", "--tail", "100", "gateway")
            throw "InfluencerResearch public gateway did not become healthy."
        }

        Write-Host "PUBLIC_GATEWAY_READY"
    }
    "Down" {
        PublicCompose -ComposeArgs @("down")
    }
    "Status" {
        PublicCompose -ComposeArgs @("ps")
    }
    "Logs" {
        PublicCompose -ComposeArgs @("logs", "--tail", "200", "gateway")
    }
}
