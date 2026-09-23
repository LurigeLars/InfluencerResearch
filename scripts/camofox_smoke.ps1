$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Python = Join-Path $env:LOCALAPPDATA 'InstagramResearch\venv\Scripts\python.exe'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$RuntimePackage = Join-Path $RepoRoot 'runtime\camofox\package.json'
$ContainerScript = Join-Path $RepoRoot 'scripts\camofox_container.ps1'
$PocScript = Join-Path $RepoRoot 'camofox_tiktok_poc.py'
$ContainerName = 'influencerresearch-camofox'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "InfluencerResearch Python environment not found: $Python"
}
if (-not (Test-Path -LiteralPath $RuntimePackage -PathType Leaf)) {
    throw "Missing runtime package manifest: $RuntimePackage"
}
if (-not (Test-Path -LiteralPath $ContainerScript -PathType Leaf)) {
    throw "Missing Camofox container script: $ContainerScript"
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI is not available on PATH.'
}

$expectedVersion = [string]((Get-Content -LiteralPath $RuntimePackage -Raw | ConvertFrom-Json).dependencies.'@askjo/camofox-browser')
if ([string]::IsNullOrWhiteSpace($expectedVersion)) {
    throw 'Unable to read expected @askjo/camofox-browser version from runtime/camofox/package.json.'
}

Push-Location $RepoRoot
try {
    Write-Host ''
    Write-Host '=== REPOSITORY ==='
    $head = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read repository HEAD.' }
    Write-Host "HEAD=$head"
    Write-Host "EXPECTED_CAMOFOX=$expectedVersion"

    Write-Host ''
    Write-Host '=== UNIT TESTS ==='
    $tests = @(
        'test_camofox_container_config',
        'test_camofox_container_runtime',
        'test_camofox_manifest_validation',
        'test_tiktok_media_transport'
    )
    & $Python -m unittest -v @tests
    if ($LASTEXITCODE -ne 0) { throw 'Camofox unit tests failed.' }

    Write-Host ''
    Write-Host '=== BUILD / START ==='
    & pwsh -NoProfile -File $ContainerScript -Action Up
    if ($LASTEXITCODE -ne 0) { throw 'Camofox container build/start failed.' }

    Write-Host ''
    Write-Host '=== VERSION READBACK ==='
    $actualVersion = (& docker exec $ContainerName node -e "console.log(require('/app/node_modules/@askjo/camofox-browser/package.json').version)").Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Camofox version readback failed.' }
    Write-Host "ACTUAL_CAMOFOX=$actualVersion"
    if ($actualVersion -ne $expectedVersion) {
        throw "Camofox version mismatch: expected $expectedVersion, got $actualVersion"
    }

    Write-Host ''
    Write-Host '=== RESOURCE LIMITS ==='
    $limits = (& docker inspect $ContainerName --format 'Memory={{.HostConfig.Memory}} NanoCpus={{.HostConfig.NanoCpus}} PidsLimit={{.HostConfig.PidsLimit}}').Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Container resource-limit readback failed.' }
    Write-Host $limits
    if ($limits -ne 'Memory=2147483648 NanoCpus=2000000000 PidsLimit=256') {
        throw "Unexpected Camofox resource limits: $limits"
    }

    Write-Host ''
    Write-Host '=== TIKTOK END-TO-END ==='
    & $Python $PocScript
    if ($LASTEXITCODE -ne 0) { throw 'TikTok/Camofox smoke failed.' }

    Write-Host ''
    Write-Host "CAMOFOX_SMOKE_PASS expected=$expectedVersion actual=$actualVersion head=$head"
}
finally {
    Pop-Location
}
