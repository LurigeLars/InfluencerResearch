param(
    [ValidateSet("Up", "Down", "Status", "Smoke", "ImportInstagramAuth")]
    [string]$Action = "Up"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA is required." }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker CLI is not available on PATH." }

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Root = (Resolve-Path (Join-Path $Repo "..")).Path
$Compose = Join-Path $Repo "compose.yaml"
$ConfigDir = Join-Path $env:LOCALAPPDATA "InfluencerResearch"
$ConfigPath = Join-Path $ConfigDir "docker-runtime.json"

New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
foreach ($name in @("control", "state", "output", "logs")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root $name) | Out-Null
}

function New-Token {
    $bytes = [byte[]]::new(32)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+","-").Replace("/","_")
}

function Save-RuntimeConfig($Config) {
    [IO.File]::WriteAllText(
        $ConfigPath,
        ($Config | ConvertTo-Json -Depth 4),
        [Text.UTF8Encoding]::new($false)
    )
}

function Test-TcpPortFree([int]$Port) {
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new(
            [System.Net.IPAddress]::Loopback,
            $Port
        )
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) {
            try { $listener.Stop() } catch {}
        }
    }
}

function Test-InfluencerResearchContainerRunning {
    $names = @(& docker ps --format "{{.Names}}")
    return $names -contains "influencerresearch-mcp"
}

function Ensure-HostMcpPort($Config) {
    if ($Action -notin @("Up", "Smoke")) {
        return
    }

    if (Test-InfluencerResearchContainerRunning) {
        return
    }

    $configuredPort = [int]$Config.mcp_port
    if (Test-TcpPortFree $configuredPort) {
        return
    }

    foreach ($candidate in 8771..8799) {
        if ($candidate -eq $configuredPort) {
            continue
        }
        if (Test-TcpPortFree $candidate) {
            Write-Host "MCP host port $configuredPort is already in use; switching to $candidate."
            $Config.mcp_port = $candidate
            Save-RuntimeConfig $Config
            return
        }
    }

    throw "MCP host port $configuredPort is already in use and no free fallback port was found in 8771-8799."
}

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    $config = [ordered]@{
        schema_version = 1
        camofox_access_key = New-Token
        camofox_admin_key = New-Token
        mcp_port = 8770
    }
    Save-RuntimeConfig $config
}

$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
if ([int]$config.schema_version -ne 1) { throw "Unsupported runtime config schema." }
if ([int]$config.mcp_port -lt 1024 -or [int]$config.mcp_port -gt 65535) {
    throw "Invalid MCP host port in runtime config."
}

Ensure-HostMcpPort $config

$env:CAMOFOX_ACCESS_KEY = [string]$config.camofox_access_key
$env:CAMOFOX_ADMIN_KEY = [string]$config.camofox_admin_key
$env:INFLUENCER_RESEARCH_MCP_PORT = [string]$config.mcp_port
$env:INFLUENCER_RESEARCH_CONTROL_DIR = Join-Path $Root "control"
$env:INFLUENCER_RESEARCH_STATE_DIR = Join-Path $Root "state"
$env:INFLUENCER_RESEARCH_OUTPUT_DIR = Join-Path $Root "output"
$env:INFLUENCER_RESEARCH_LOG_DIR = Join-Path $Root "logs"

function Compose([string[]]$ComposeArgs) {
    & docker compose -f $Compose @ComposeArgs
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }
}

function Import-InstagramAuth {
    $cookiePath = Join-Path $env:LOCALAPPDATA "InfluencerResearch\secrets\instagram_cookies.json"
    if (-not (Test-Path -LiteralPath $cookiePath -PathType Leaf)) {
        throw "Instagram cookie export is missing. Run scripts\authenticate_instagram.ps1 first."
    }

    $cookies = @(Get-Content -LiteralPath $cookiePath -Raw | ConvertFrom-Json)
    if (-not ($cookies | Where-Object { [string]$_.name -eq "sessionid" })) {
        throw "Instagram cookie export does not contain sessionid."
    }

    $serviceId = (& docker compose -f $Compose ps -q influencerresearch).Trim()
    if (-not $serviceId) {
        throw "InfluencerResearch container is not running. Run scripts\runtime.ps1 -Action Up first."
    }

    Get-Content -LiteralPath $cookiePath -Raw |
        & docker compose -f $Compose exec -T influencerresearch sh -c 'umask 077; mkdir -p /runtime/influencerresearch/secrets; cat > /runtime/influencerresearch/secrets/instagram_cookies.json'
    if ($LASTEXITCODE -ne 0) { throw "Instagram auth import failed." }

    & docker compose -f $Compose exec -T influencerresearch python -c 'import json; p="/runtime/influencerresearch/secrets/instagram_cookies.json"; c=json.load(open(p,encoding="utf-8")); assert any(x.get("name")=="sessionid" for x in c); print("INSTAGRAM_AUTH_IMPORTED")'
    if ($LASTEXITCODE -ne 0) { throw "Instagram auth verification failed." }
}

switch ($Action) {
    "Up" {
        Compose -ComposeArgs @("up", "-d", "--build")
        Write-Host "INFLUENCERRESEARCH_MCP=http://127.0.0.1:$($config.mcp_port)/mcp"
        Write-Host "Camofox is internal-only at http://camofox:9377"
        $cookiePath = Join-Path $env:LOCALAPPDATA "InfluencerResearch\secrets\instagram_cookies.json"
        if (Test-Path -LiteralPath $cookiePath -PathType Leaf) {
            Import-InstagramAuth
        }
    }
    "Down" {
        Compose -ComposeArgs @("down")
    }
    "ImportInstagramAuth" {
        Import-InstagramAuth
    }
    "Status" {
        Compose -ComposeArgs @("ps")
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($config.mcp_port)/health" -TimeoutSec 3
            $health | ConvertTo-Json -Depth 5
        } catch {
            Write-Warning "MCP health endpoint is not reachable."
        }
    }
    "Smoke" {
        Compose -ComposeArgs @("up", "-d", "--build")
        $tests = @(
            "test_camofox_container_config",
            "test_camofox_container_runtime",
            "test_camofox_manifest_validation",
            "test_tiktok_media_transport",
            "test_smoke_production_separation",
            "test_mcp_contract",
            "test_local_runtime_namespace"
        )
        & docker compose -f $Compose exec -T influencerresearch python -m unittest -v @tests
        if ($LASTEXITCODE -ne 0) { throw "Container unit smoke failed." }
        & docker compose -f $Compose exec -T influencerresearch python tiktok_camofox_smoke.py
        if ($LASTEXITCODE -ne 0) { throw "TikTok/Camofox smoke failed." }
    }
}
