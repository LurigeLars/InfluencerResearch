param(
    [ValidateSet("Up", "Down", "Status", "Smoke")]
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

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    $config = [ordered]@{
        schema_version = 1
        camofox_access_key = New-Token
        camofox_admin_key = New-Token
        mcp_port = 8770
    }
    [IO.File]::WriteAllText($ConfigPath, ($config | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
}

$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
if ([int]$config.schema_version -ne 1) { throw "Unsupported runtime config schema." }

$env:CAMOFOX_ACCESS_KEY = [string]$config.camofox_access_key
$env:CAMOFOX_ADMIN_KEY = [string]$config.camofox_admin_key
$env:INFLUENCER_RESEARCH_MCP_PORT = [string]$config.mcp_port
$env:INFLUENCER_RESEARCH_CONTROL_DIR = Join-Path $Root "control"
$env:INFLUENCER_RESEARCH_STATE_DIR = Join-Path $Root "state"
$env:INFLUENCER_RESEARCH_OUTPUT_DIR = Join-Path $Root "output"
$env:INFLUENCER_RESEARCH_LOG_DIR = Join-Path $Root "logs"

function Compose([string[]]$Args) {
    & docker compose -f $Compose @Args
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }
}

switch ($Action) {
    "Up" {
        Compose @("up", "-d", "--build")
        Write-Host "INFLUENCERRESEARCH_MCP=http://127.0.0.1:$($config.mcp_port)/mcp"
        Write-Host "Camofox is internal-only at http://camofox:9377"
    }
    "Down" {
        Compose @("down")
    }
    "Status" {
        Compose @("ps")
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($config.mcp_port)/health" -TimeoutSec 3
            $health | ConvertTo-Json -Depth 5
        } catch {
            Write-Warning "MCP health endpoint is not reachable."
        }
    }
    "Smoke" {
        Compose @("up", "-d", "--build")
        $tests = @(
            "test_camofox_container_config",
            "test_camofox_container_runtime",
            "test_camofox_manifest_validation",
            "test_tiktok_media_transport",
            "test_smoke_production_separation",
            "test_mcp_contract"
        )
        & docker compose -f $Compose exec -T influencerresearch python -m unittest -v @tests
        if ($LASTEXITCODE -ne 0) { throw "Container unit smoke failed." }
        & docker compose -f $Compose exec -T influencerresearch python tiktok_camofox_smoke.py
        if ($LASTEXITCODE -ne 0) { throw "TikTok/Camofox smoke failed." }
    }
}
