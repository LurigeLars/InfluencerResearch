$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ExpectedNode = 'v22.23.2'
$ExpectedCamofox = '1.13.1'
$ExpectedCamoufoxJs = '0.11.5'
$ExpectedBrowserVersion = '152.0.4'
$ExpectedBrowserRelease = 'beta.28'

if ($env:OS -ne 'Windows_NT') {
    throw 'This reviewed Camofox runtime installer currently supports Windows only.'
}
if (-not $env:LOCALAPPDATA) {
    throw 'LOCALAPPDATA is required.'
}
foreach ($cmd in @('node', 'npm')) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $cmd"
    }
}

$actualNode = (& node --version).Trim()
if ($LASTEXITCODE -ne 0 -or $actualNode -ne $ExpectedNode) {
    throw "Expected Node.js $ExpectedNode, got '$actualNode'."
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ManifestDir = Join-Path $RepoRoot 'runtime\camofox'
$SourcePackage = Join-Path $ManifestDir 'package.json'
$SourceLock = Join-Path $ManifestDir 'package-lock.json'
$RuntimeRoot = Join-Path $env:LOCALAPPDATA 'InstagramResearch'
$Dest = Join-Path $RuntimeRoot 'camofox-poc'
$Stage = Join-Path $RuntimeRoot ('camofox-poc.stage-' + [guid]::NewGuid().ToString('N'))
$Backup = Join-Path $RuntimeRoot ('camofox-poc.backup-' + [guid]::NewGuid().ToString('N'))

foreach ($path in @($SourcePackage, $SourceLock)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required manifest missing: $path"
    }
}

New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
Copy-Item -LiteralPath $SourcePackage -Destination (Join-Path $Stage 'package.json') -Force
Copy-Item -LiteralPath $SourceLock -Destination (Join-Path $Stage 'package-lock.json') -Force

$SensitiveEnv = @(
    'GITHUB_TOKEN', 'GH_TOKEN', 'NODE_AUTH_TOKEN', 'NPM_TOKEN',
    'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GOOGLE_API_KEY',
    'CLOUDFLARE_API_TOKEN', 'CF_API_TOKEN'
)
$SavedEnv = @{}
foreach ($name in $SensitiveEnv) {
    $SavedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    [Environment]::SetEnvironmentVariable($name, $null, 'Process')
}

$StageValidated = $false
try {
    Push-Location $Stage
    try {
        Write-Host 'Installing locked Node dependencies into staging with lifecycle scripts disabled...'
        & npm ci --ignore-scripts --omit=dev --omit=optional --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE." }

        Write-Host 'Building the reviewed better-sqlite3 native dependency...'
        & npm rebuild better-sqlite3 --foreground-scripts --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "better-sqlite3 rebuild failed with exit code $LASTEXITCODE." }

        $PostInstall = Join-Path $Stage 'node_modules\@askjo\camofox-browser\scripts\postinstall.js'
        if (-not (Test-Path -LiteralPath $PostInstall -PathType Leaf)) {
            throw "Reviewed Camofox postinstall script not found: $PostInstall"
        }
        Write-Host 'Fetching/verifying the reviewed Camoufox browser baseline...'
        & node $PostInstall
        if ($LASTEXITCODE -ne 0) { throw "Camofox postinstall failed with exit code $LASTEXITCODE." }
    }
    finally {
        Pop-Location
    }

    $VersionCheck = "const fs=require('fs');const path=require('path');const root=process.argv[1];const expected={'@askjo/camofox-browser':process.argv[2],'camoufox-js':process.argv[3]};for(const [name,version] of Object.entries(expected)){const p=path.join(root,'node_modules',...name.split('/'),'package.json');const actual=JSON.parse(fs.readFileSync(p,'utf8')).version;if(actual!==version)throw new Error(name+': expected '+version+', got '+actual);}"
    & node -e $VersionCheck $Stage $ExpectedCamofox $ExpectedCamoufoxJs
    if ($LASTEXITCODE -ne 0) { throw 'Installed package version verification failed.' }

    $VersionFile = Join-Path $env:LOCALAPPDATA 'camoufox\camoufox\Cache\version.json'
    if (-not (Test-Path -LiteralPath $VersionFile -PathType Leaf)) {
        throw "Camoufox version file missing after install: $VersionFile"
    }
    $BrowserVersion = Get-Content -LiteralPath $VersionFile -Raw | ConvertFrom-Json
    if ([string]$BrowserVersion.version -ne $ExpectedBrowserVersion -or [string]$BrowserVersion.release -ne $ExpectedBrowserRelease) {
        throw "Unexpected Camoufox browser baseline: version=$($BrowserVersion.version) release=$($BrowserVersion.release)"
    }

    $Cli = Join-Path $Stage 'node_modules\.bin\camofox-browser.cmd'
    if (-not (Test-Path -LiteralPath $Cli -PathType Leaf)) {
        throw "Camofox CLI missing after staged install: $Cli"
    }

    $StageValidated = $true
}
finally {
    foreach ($name in $SensitiveEnv) {
        [Environment]::SetEnvironmentVariable($name, $SavedEnv[$name], 'Process')
    }
    if (-not $StageValidated -and (Test-Path -LiteralPath $Stage)) {
        Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$HadExistingRuntime = Test-Path -LiteralPath $Dest
try {
    if ($HadExistingRuntime) {
        Write-Host 'Staging verified. Moving current runtime aside...'
        Move-Item -LiteralPath $Dest -Destination $Backup
    }

    Write-Host 'Activating verified staged runtime...'
    Move-Item -LiteralPath $Stage -Destination $Dest
}
catch {
    $SwapError = $_
    if (Test-Path -LiteralPath $Stage) {
        Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($HadExistingRuntime -and (Test-Path -LiteralPath $Backup) -and -not (Test-Path -LiteralPath $Dest)) {
        Move-Item -LiteralPath $Backup -Destination $Dest
    }
    throw "Camofox runtime activation failed; previous runtime was preserved when possible. $($SwapError.Exception.Message)"
}

if (Test-Path -LiteralPath $Backup) {
    Remove-Item -LiteralPath $Backup -Recurse -Force
}

Write-Host 'Camofox runtime installed and verified.'
Write-Host "Runtime: $Dest"
Write-Host "Node: $actualNode"
Write-Host "Camofox Browser: $ExpectedCamofox"
Write-Host "camoufox-js: $ExpectedCamoufoxJs"
Write-Host "Camoufox browser: $ExpectedBrowserVersion / $ExpectedBrowserRelease"
