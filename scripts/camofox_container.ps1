param(
    [ValidateSet('Up', 'Down', 'Status')]
    [string]$Action = 'Up'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is required.' }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker CLI is not available on PATH.' }

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Compose = Join-Path $RepoRoot 'compose.camofox.yaml'
if (-not (Test-Path -LiteralPath $Compose -PathType Leaf)) { throw "Missing compose file: $Compose" }

$LocalRoot = Join-Path $env:LOCALAPPDATA 'InfluencerResearch'
$ConfigPath = Join-Path $LocalRoot 'camofox-container.json'
New-Item -ItemType Directory -Force -Path $LocalRoot | Out-Null

function New-LocalToken {
    $bytes = [byte[]]::new(32)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
}

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    $newConfig = [ordered]@{
        schema_version = 1
        access_key = New-LocalToken
        admin_key = New-LocalToken
    }
    [IO.File]::WriteAllText(
        $ConfigPath,
        ($newConfig | ConvertTo-Json -Depth 4),
        [Text.UTF8Encoding]::new($false)
    )
}

$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
if ([int]$config.schema_version -ne 1) { throw "Unsupported config schema: $($config.schema_version)" }
if ([string]::IsNullOrWhiteSpace([string]$config.access_key) -or ([string]$config.access_key).Length -lt 32) { throw 'Camofox access key is invalid.' }
if ([string]::IsNullOrWhiteSpace([string]$config.admin_key) -or ([string]$config.admin_key).Length -lt 32) { throw 'Camofox admin key is invalid.' }

$env:CAMOFOX_ACCESS_KEY = [string]$config.access_key
$env:CAMOFOX_ADMIN_KEY = [string]$config.admin_key

function Invoke-Compose([string[]]$Arguments) {
    & docker compose -f $Compose @Arguments
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE." }
}

function Assert-AuthenticatedHealth {
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        try {
            $health = Invoke-RestMethod -Uri 'http://127.0.0.1:9377/health' -TimeoutSec 3
            $headers = @{ Authorization = "Bearer $($config.access_key)" }
            [void](Invoke-RestMethod -Uri 'http://127.0.0.1:9377/tabs?userId=container-health-probe' -Headers $headers -TimeoutSec 3)
            return $health
        } catch {
            Start-Sleep -Milliseconds 500
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Camofox container did not become healthy with authenticated API access.'
}

switch ($Action) {
    'Up' {
        Invoke-Compose @('up', '-d', '--build')
        $health = Assert-AuthenticatedHealth
        Write-Host 'CAMOFOX_CONTAINER_READY'
        $health | ConvertTo-Json -Depth 6
        Write-Host "Config: $ConfigPath"
    }
    'Down' {
        Invoke-Compose @('down')
        Write-Host 'CAMOFOX_CONTAINER_STOPPED'
    }
    'Status' {
        Invoke-Compose @('ps')
        $health = Assert-AuthenticatedHealth
        $health | ConvertTo-Json -Depth 6
    }
}
