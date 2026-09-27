param(
    [ValidateSet("Up", "Redeploy", "Down", "Status", "Smoke", "InstagramPublicSmoke", "ImportInstagramAuth", "ImportGeminiKey")]
    [string]$Action = "Up",
    [string]$InstagramProfileUrl = "https://www.instagram.com/rikatillsammans/",
    [string]$InstagramHandle = "rikatillsammans",
    [ValidateRange(1, 5)]
    [int]$InstagramRuns = 3
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
$SecretDir = Join-Path $ConfigDir "secrets"
$CamofoxAccessDpapiPath = Join-Path $SecretDir "camofox_access_key.dpapi"
$CamofoxAdminDpapiPath = Join-Path $SecretDir "camofox_admin_key.dpapi"
$InstagramDpapiPath = Join-Path $SecretDir "instagram_cookies.dpapi"
$LegacyInstagramPath = Join-Path $SecretDir "instagram_cookies.json"

New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
New-Item -ItemType Directory -Force -Path $SecretDir | Out-Null
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

function Save-DpapiSecret([string]$Path, [string]$Value, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Label secret is empty."
    }

    $secure = ConvertTo-SecureString -String $Value -AsPlainText -Force
    try {
        $encrypted = ConvertFrom-SecureString -SecureString $secure
        [IO.File]::WriteAllText($Path, $encrypted, [Text.UTF8Encoding]::new($false))
    }
    finally {
        $secure = $null
    }

    $roundTrip = Get-DpapiSecretValue -Path $Path -Label $Label
    try {
        if ($roundTrip -ne $Value) {
            throw "$Label DPAPI verification failed."
        }
    }
    finally {
        $roundTrip = $null
    }
}

function Get-DpapiSecretValue([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label DPAPI secret is missing."
    }

    $encrypted = Get-Content -LiteralPath $Path -Raw
    $secure = ConvertTo-SecureString -String $encrypted
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
        if ([string]::IsNullOrWhiteSpace($plain)) {
            throw "$Label DPAPI secret decrypted to an empty value."
        }
        return $plain
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
        $secure = $null
    }
}

function Ensure-CamofoxSecretStore($Config) {
    $legacyAccess = $null
    $legacyAdmin = $null
    if ($Config.PSObject.Properties.Name -contains "camofox_access_key") {
        $legacyAccess = [string]$Config.camofox_access_key
    }
    if ($Config.PSObject.Properties.Name -contains "camofox_admin_key") {
        $legacyAdmin = [string]$Config.camofox_admin_key
    }

    if (-not (Test-Path -LiteralPath $CamofoxAccessDpapiPath -PathType Leaf)) {
        $value = if (-not [string]::IsNullOrWhiteSpace($legacyAccess)) { $legacyAccess } else { New-Token }
        try {
            Save-DpapiSecret -Path $CamofoxAccessDpapiPath -Value $value -Label "Camofox access"
        }
        finally {
            $value = $null
        }
    }

    if (-not (Test-Path -LiteralPath $CamofoxAdminDpapiPath -PathType Leaf)) {
        $value = if (-not [string]::IsNullOrWhiteSpace($legacyAdmin)) { $legacyAdmin } else { New-Token }
        try {
            Save-DpapiSecret -Path $CamofoxAdminDpapiPath -Value $value -Label "Camofox admin"
        }
        finally {
            $value = $null
        }
    }

    $storedAccess = $null
    $storedAdmin = $null
    try {
        $storedAccess = Get-DpapiSecretValue -Path $CamofoxAccessDpapiPath -Label "Camofox access"
        $storedAdmin = Get-DpapiSecretValue -Path $CamofoxAdminDpapiPath -Label "Camofox admin"
        if (-not [string]::IsNullOrWhiteSpace($legacyAccess) -and $storedAccess -ne $legacyAccess) {
            throw "Camofox access DPAPI secret does not match the legacy runtime config; refusing to remove the legacy value."
        }
        if (-not [string]::IsNullOrWhiteSpace($legacyAdmin) -and $storedAdmin -ne $legacyAdmin) {
            throw "Camofox admin DPAPI secret does not match the legacy runtime config; refusing to remove the legacy value."
        }
    }
    finally {
        $storedAccess = $null
        $storedAdmin = $null
        $legacyAccess = $null
        $legacyAdmin = $null
    }
}

function Test-InstagramCookieJson([string]$Json) {
    try {
        $cookies = @($Json | ConvertFrom-Json)
    }
    catch {
        throw "Instagram cookie payload is not valid JSON."
    }
    if (-not ($cookies | Where-Object { [string]$_.name -eq "sessionid" })) {
        throw "Instagram cookie payload does not contain sessionid."
    }
}

function Ensure-InstagramCookieStore {
    $legacy = $null
    if (Test-Path -LiteralPath $LegacyInstagramPath -PathType Leaf) {
        $legacy = Get-Content -LiteralPath $LegacyInstagramPath -Raw
        Test-InstagramCookieJson $legacy
    }

    if (-not (Test-Path -LiteralPath $InstagramDpapiPath -PathType Leaf)) {
        if ([string]::IsNullOrWhiteSpace($legacy)) {
            return $false
        }
        Save-DpapiSecret -Path $InstagramDpapiPath -Value $legacy -Label "Instagram session"
    }

    $stored = $null
    try {
        $stored = Get-DpapiSecretValue -Path $InstagramDpapiPath -Label "Instagram session"
        Test-InstagramCookieJson $stored
        if (-not [string]::IsNullOrWhiteSpace($legacy) -and $stored -ne $legacy) {
            throw "Instagram DPAPI session does not match the legacy cookie export; refusing to remove the legacy file."
        }
    }
    finally {
        $stored = $null
    }

    if (-not [string]::IsNullOrWhiteSpace($legacy)) {
        Remove-Item -LiteralPath $LegacyInstagramPath -Force
    }
    $legacy = $null
    return $true
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
    if ($Action -notin @("Up", "Redeploy", "Smoke", "InstagramPublicSmoke")) {
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

$needsCamofoxSecrets = $Action -in @("Up", "Redeploy", "Smoke", "InstagramPublicSmoke")

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    $config = [pscustomobject]@{
        schema_version = 2
        mcp_port = 8770
    }
    Save-RuntimeConfig $config
} else {
    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    $schemaVersion = [int]$config.schema_version
    if ($schemaVersion -notin @(1, 2)) { throw "Unsupported runtime config schema." }

    if ($needsCamofoxSecrets) {
        Ensure-CamofoxSecretStore $config

        if ($schemaVersion -eq 1 -or
            $config.PSObject.Properties.Name -contains "camofox_access_key" -or
            $config.PSObject.Properties.Name -contains "camofox_admin_key") {
            $config = [pscustomobject]@{
                schema_version = 2
                mcp_port = [int]$config.mcp_port
            }
            Save-RuntimeConfig $config
        }
    }
}

if ($needsCamofoxSecrets -and [int]$config.schema_version -eq 2) {
    Ensure-CamofoxSecretStore $config
}

if ([int]$config.mcp_port -lt 1024 -or [int]$config.mcp_port -gt 65535) {
    throw "Invalid MCP host port in runtime config."
}

Ensure-HostMcpPort $config

$env:INFLUENCER_RESEARCH_MCP_PORT = [string]$config.mcp_port
$env:INFLUENCER_RESEARCH_CONTROL_DIR = Join-Path $Root "control"
$env:INFLUENCER_RESEARCH_STATE_DIR = Join-Path $Root "state"
$env:INFLUENCER_RESEARCH_OUTPUT_DIR = Join-Path $Root "output"
$env:INFLUENCER_RESEARCH_LOG_DIR = Join-Path $Root "logs"

function Compose([string[]]$ComposeArgs) {
    & docker compose -f $Compose @ComposeArgs
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }
}

function Invoke-ComposeUp([bool]$ForceRecreate = $false) {
    $accessWasSet = Test-Path Env:INFLUENCER_CAMOFOX_ACCESS_SECRET
    $adminWasSet = Test-Path Env:INFLUENCER_CAMOFOX_ADMIN_SECRET
    $oldAccess = if ($accessWasSet) { $env:INFLUENCER_CAMOFOX_ACCESS_SECRET } else { $null }
    $oldAdmin = if ($adminWasSet) { $env:INFLUENCER_CAMOFOX_ADMIN_SECRET } else { $null }

    $access = $null
    $admin = $null
    try {
        $access = Get-DpapiSecretValue -Path $CamofoxAccessDpapiPath -Label "Camofox access"
        $admin = Get-DpapiSecretValue -Path $CamofoxAdminDpapiPath -Label "Camofox admin"
        $env:INFLUENCER_CAMOFOX_ACCESS_SECRET = $access
        $env:INFLUENCER_CAMOFOX_ADMIN_SECRET = $admin
        $composeArgs = @("up", "-d", "--build")
        if ($ForceRecreate) { $composeArgs += "--force-recreate" }
        Compose -ComposeArgs $composeArgs
    }
    finally {
        $access = $null
        $admin = $null
        if ($accessWasSet) {
            $env:INFLUENCER_CAMOFOX_ACCESS_SECRET = $oldAccess
        } else {
            Remove-Item Env:INFLUENCER_CAMOFOX_ACCESS_SECRET -ErrorAction SilentlyContinue
        }
        if ($adminWasSet) {
            $env:INFLUENCER_CAMOFOX_ADMIN_SECRET = $oldAdmin
        } else {
            Remove-Item Env:INFLUENCER_CAMOFOX_ADMIN_SECRET -ErrorAction SilentlyContinue
        }
    }
}

function Import-InstagramAuth {
    if (-not (Ensure-InstagramCookieStore)) {
        throw "Instagram DPAPI session is missing. Run scripts\authenticate_instagram.ps1 first."
    }

    $serviceId = (& docker compose -f $Compose ps -q influencerresearch).Trim()
    if (-not $serviceId) {
        throw "InfluencerResearch container is not running. Run scripts\runtime.ps1 -Action Up first."
    }

    $plain = $null
    try {
        $plain = Get-DpapiSecretValue -Path $InstagramDpapiPath -Label "Instagram session"
        Test-InstagramCookieJson $plain
        $plain |
            & docker compose -f $Compose exec -T influencerresearch sh -c 'umask 077; cat > /run/influencerresearch-secrets/instagram_cookies.json'
        if ($LASTEXITCODE -ne 0) { throw "Instagram auth import failed." }
    }
    finally {
        $plain = $null
    }

    & docker compose -f $Compose exec -T influencerresearch python -c 'import json; p="/run/influencerresearch-secrets/instagram_cookies.json"; c=json.load(open(p,encoding="utf-8")); assert any(x.get("name")=="sessionid" for x in c); print("INSTAGRAM_AUTH_IMPORTED")'
    if ($LASTEXITCODE -ne 0) { throw "Instagram auth verification failed." }
}

function Import-GeminiKey {
    $secretPath = Join-Path $env:LOCALAPPDATA "InfluencerResearch\secrets\gemini_api_key.dpapi"
    if (-not (Test-Path -LiteralPath $secretPath -PathType Leaf)) {
        throw "Gemini DPAPI secret is missing. Run scripts\configure_gemini.ps1 first."
    }

    $serviceId = (& docker compose -f $Compose ps -q influencerresearch).Trim()
    if (-not $serviceId) {
        throw "InfluencerResearch container is not running. Run scripts\runtime.ps1 -Action Up first."
    }

    $encrypted = Get-Content -LiteralPath $secretPath -Raw
    $secure = ConvertTo-SecureString -String $encrypted
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $plain = $null
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
        if ([string]::IsNullOrWhiteSpace($plain)) {
            throw "Gemini DPAPI secret decrypted to an empty value."
        }

        $plain |
            & docker compose -f $Compose exec -T influencerresearch sh -c 'umask 077; cat > /run/influencerresearch-secrets/gemini_api_key'
        if ($LASTEXITCODE -ne 0) { throw "Gemini key import failed." }
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
        $plain = $null
        $secure = $null
    }

    & docker compose -f $Compose exec -T influencerresearch sh -c 'test -s /run/influencerresearch-secrets/gemini_api_key && printf "GEMINI_KEY_IMPORTED\n"'
    if ($LASTEXITCODE -ne 0) { throw "Gemini key verification failed." }
}

function Import-AvailableRuntimeSecrets {
    if (
        (Test-Path -LiteralPath $InstagramDpapiPath -PathType Leaf) -or
        (Test-Path -LiteralPath $LegacyInstagramPath -PathType Leaf)
    ) {
        Import-InstagramAuth
    }

    $geminiPath = Join-Path $env:LOCALAPPDATA "InfluencerResearch\secrets\gemini_api_key.dpapi"
    if (Test-Path -LiteralPath $geminiPath -PathType Leaf) {
        Import-GeminiKey
    }
}

$outerAccessWasSet = Test-Path Env:INFLUENCER_CAMOFOX_ACCESS_SECRET
$outerAdminWasSet = Test-Path Env:INFLUENCER_CAMOFOX_ADMIN_SECRET
$outerAccessOriginal = if ($outerAccessWasSet) { $env:INFLUENCER_CAMOFOX_ACCESS_SECRET } else { $null }
$outerAdminOriginal = if ($outerAdminWasSet) { $env:INFLUENCER_CAMOFOX_ADMIN_SECRET } else { $null }

try {
    # Compose reparses post_start interpolation for ps/exec/status/down too. Keep
    # inert placeholders present outside up/redeploy so those commands do not
    # require or expose the real Camofox secrets. Invoke-ComposeUp temporarily
    # replaces these placeholders with the DPAPI-decrypted values only while
    # the post_start hooks write them into tmpfs.
    $env:INFLUENCER_CAMOFOX_ACCESS_SECRET = "compose-config-only"
    $env:INFLUENCER_CAMOFOX_ADMIN_SECRET = "compose-config-only"

switch ($Action) {
    "Up" {
        Invoke-ComposeUp
        Write-Host "INFLUENCERRESEARCH_MCP=http://127.0.0.1:$($config.mcp_port)/mcp"
        Write-Host "Camofox is internal-only at http://camofox:9377"
        Import-AvailableRuntimeSecrets
    }
    "Redeploy" {
        Invoke-ComposeUp -ForceRecreate $true
        Write-Host "INFLUENCERRESEARCH_MCP=http://127.0.0.1:$($config.mcp_port)/mcp"
        Write-Host "Camofox is internal-only at http://camofox:9377"
        Import-AvailableRuntimeSecrets
    }
    "Down" {
        Compose -ComposeArgs @("down")
    }
    "InstagramPublicSmoke" {
        Invoke-ComposeUp
        & docker exec influencerresearch-mcp `
            python -m unittest -v test_instagram_camofox_public_smoke
        if ($LASTEXITCODE -ne 0) { throw "Instagram public smoke unit tests failed." }

        & docker exec influencerresearch-mcp `
            python /research/app/instagram_camofox_public_smoke.py `
            --profile-url $InstagramProfileUrl `
            --handle $InstagramHandle `
            --runs $InstagramRuns
        if ($LASTEXITCODE -ne 0) { throw "Instagram public Camofox smoke failed." }
    }
    "ImportInstagramAuth" {
        Import-InstagramAuth
    }
    "ImportGeminiKey" {
        Import-GeminiKey
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
        Invoke-ComposeUp
        Import-AvailableRuntimeSecrets
        $tests = @(
            "test_camofox_container_config",
            "test_camofox_container_runtime",
            "test_camofox_manifest_validation",
            "test_tiktok_media_transport",
            "test_smoke_production_separation",
            "test_mcp_contract",
            "test_local_runtime_namespace",
            "test_transcription_backend",
            "test_instagram_ingest_limits"
        )
        & docker compose -f $Compose exec -T influencerresearch python -m unittest -v @tests
        if ($LASTEXITCODE -ne 0) { throw "Container unit smoke failed." }
        & docker compose -f $Compose exec -T influencerresearch python tiktok_camofox_smoke.py
        if ($LASTEXITCODE -ne 0) { throw "TikTok/Camofox smoke failed." }
    }
}
}
finally {
    if ($outerAccessWasSet) {
        $env:INFLUENCER_CAMOFOX_ACCESS_SECRET = $outerAccessOriginal
    } else {
        Remove-Item Env:INFLUENCER_CAMOFOX_ACCESS_SECRET -ErrorAction SilentlyContinue
    }
    if ($outerAdminWasSet) {
        $env:INFLUENCER_CAMOFOX_ADMIN_SECRET = $outerAdminOriginal
    } else {
        Remove-Item Env:INFLUENCER_CAMOFOX_ADMIN_SECRET -ErrorAction SilentlyContinue
    }
}
