param(
    [ValidateSet("Up", "Redeploy", "Recover", "Down", "Status", "Smoke", "InstagramPublicSmoke", "ImportInstagramAuth", "ImportGeminiKey")]
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
$FirecrawlPublicProxyUsernameDpapiPath = Join-Path $env:LOCALAPPDATA "FirecrawlLocal\secrets\public_proxy_username.dpapi"
$FirecrawlPublicProxyPasswordDpapiPath = Join-Path $env:LOCALAPPDATA "FirecrawlLocal\secrets\public_proxy_password.dpapi"
$InstagramDpapiPath = Join-Path $SecretDir "instagram_cookies.dpapi"
$LegacyInstagramPath = Join-Path $SecretDir "instagram_cookies.json"
$SupervisorConfigPath = Join-Path $env:LOCALAPPDATA "DockerLocalMCP\runtime-supervisor.local.json"
$SecretHolder = "influencerresearch-secret-holder"
$RuntimeScriptPath = (Resolve-Path -LiteralPath $PSCommandPath).Path

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

function Test-PublicProxyConfigured {
    return (
        (Test-Path -LiteralPath $FirecrawlPublicProxyUsernameDpapiPath -PathType Leaf) -and
        (Test-Path -LiteralPath $FirecrawlPublicProxyPasswordDpapiPath -PathType Leaf)
    )
}

function Get-PublicProxyRuntimeUsername {
    $username = Get-DpapiSecretValue -Path $FirecrawlPublicProxyUsernameDpapiPath -Label "Firecrawl public proxy username"
    if ($username.EndsWith("-rotate", [StringComparison]::OrdinalIgnoreCase)) {
        return $username
    }
    return "$username-rotate"
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
    if ($Action -notin @("Up", "Redeploy", "Recover", "Smoke", "InstagramPublicSmoke")) {
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

$needsCamofoxSecrets = $Action -in @("Up", "Redeploy", "Recover", "Smoke", "InstagramPublicSmoke")
$publicProxyConfigured = Test-PublicProxyConfigured

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

function Invoke-DockerWithExactStdin {
    param(
        [Parameter(Mandatory)][string]$InputText,
        [Parameter(Mandatory)][string[]]$Arguments
    )

    $dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
    if (-not $dockerCommand) {
        $dockerCommand = Get-Command docker -ErrorAction Stop
    }

    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $dockerCommand.Source
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true

    foreach ($argument in $Arguments) {
        [void]$psi.ArgumentList.Add([string]$argument)
    }

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $psi
    try {
        [void]$process.Start()
        $process.StandardInput.Write($InputText)
        $process.StandardInput.Close()
        $stdout = $process.StandardOutput.ReadToEnd()
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw "Docker stdin operation failed with exit code $($process.ExitCode): $stderr"
        }
        return $stdout
    }
    finally {
        if (-not $process.HasExited) {
            try { $process.Kill($true) } catch {}
        }
        $process.Dispose()
    }
}

function Write-SecretHolderFile([string]$Value, [string]$Path, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Label secret is empty."
    }
    if ($Path -notmatch '^/run/secret-store/(mcp|camofox|proxy)/[A-Za-z0-9_.-]+$') {
        throw "Unexpected runtime secret path: $Path"
    }

    [void](Invoke-DockerWithExactStdin -InputText $Value -Arguments @(
        "exec", "-i", $SecretHolder,
        "sh", "-c",
        "umask 027; cat > $Path"
    ))
}

function Materialize-CamofoxRuntimeSecrets {
    $access = $null
    $admin = $null
    $proxyUser = $null
    $proxyPass = $null
    try {
        $access = Get-DpapiSecretValue -Path $CamofoxAccessDpapiPath -Label "Camofox access"
        $admin = Get-DpapiSecretValue -Path $CamofoxAdminDpapiPath -Label "Camofox admin"

        Write-SecretHolderFile -Value $access -Path "/run/secret-store/mcp/camofox_access_key" -Label "Camofox access"
        Write-SecretHolderFile -Value $admin -Path "/run/secret-store/mcp/camofox_admin_key" -Label "Camofox admin"
        Write-SecretHolderFile -Value $access -Path "/run/secret-store/camofox/access_key" -Label "Camofox access"
        Write-SecretHolderFile -Value $admin -Path "/run/secret-store/camofox/admin_key" -Label "Camofox admin"

        if ($publicProxyConfigured) {
            $proxyUser = Get-PublicProxyRuntimeUsername
            $proxyPass = Get-DpapiSecretValue -Path $FirecrawlPublicProxyPasswordDpapiPath -Label "Firecrawl public proxy password"
            Write-SecretHolderFile -Value $access -Path "/run/secret-store/proxy/access_key" -Label "Camofox access"
            Write-SecretHolderFile -Value $admin -Path "/run/secret-store/proxy/admin_key" -Label "Camofox admin"
            Write-SecretHolderFile -Value $proxyUser -Path "/run/secret-store/proxy/proxy_username" -Label "Public proxy username"
            Write-SecretHolderFile -Value $proxyPass -Path "/run/secret-store/proxy/proxy_password" -Label "Public proxy password"
        }
    }
    finally {
        $access = $null
        $admin = $null
        $proxyUser = $null
        $proxyPass = $null
    }
}

function Invoke-ComposeUp([bool]$Build = $true) {
    Compose -ComposeArgs @("up", "-d", "secret-holder")
    Materialize-CamofoxRuntimeSecrets
    Import-AvailableRuntimeSecrets

    $profileArgs = @()
    if ($publicProxyConfigured) {
        $profileArgs += @("--profile", "public-proxy")
    }

    if (-not $Build) {
        Compose -ComposeArgs @($profileArgs + @("up", "-d"))
        return
    }

    $buildServices = @("influencerresearch", "camofox")
    if ($publicProxyConfigured) {
        $buildServices += "camofox-public-proxy"
    }

    Compose -ComposeArgs @($profileArgs + @("build") + $buildServices)

    $camofoxServices = @("camofox")
    if ($publicProxyConfigured) {
        $camofoxServices += "camofox-public-proxy"
    }

    # The build can produce a new local image ID even when service configuration
    # is unchanged. Recreate the services whose images were just built, but do
    # not recreate secret-holder: its tmpfs volumes were hydrated immediately
    # above and must remain intact.
    Compose -ComposeArgs @($profileArgs + @("up", "-d", "--no-deps", "--force-recreate") + $camofoxServices)

    # Normal Compose dependency handling now waits for Camofox health before
    # recreating/starting the MCP service from its freshly built image.
    Compose -ComposeArgs @($profileArgs + @("up", "-d", "influencerresearch"))
}

function Import-InstagramAuth {
    if (-not (Ensure-InstagramCookieStore)) {
        throw "Instagram DPAPI session is missing. Run scripts\authenticate_instagram.ps1 first."
    }

    Compose -ComposeArgs @("up", "-d", "secret-holder")

    $plain = $null
    try {
        $plain = Get-DpapiSecretValue -Path $InstagramDpapiPath -Label "Instagram session"
        Test-InstagramCookieJson $plain
        Write-SecretHolderFile -Value $plain -Path "/run/secret-store/mcp/instagram_cookies.json" -Label "Instagram session"
    }
    finally {
        $plain = $null
    }

    & docker exec $SecretHolder test -s /run/secret-store/mcp/instagram_cookies.json
    if ($LASTEXITCODE -ne 0) { throw "Instagram auth verification failed." }
    Write-Host "INSTAGRAM_AUTH_IMPORTED"
}

function Import-GeminiKey {
    $secretPath = Join-Path $env:LOCALAPPDATA "InfluencerResearch\secrets\gemini_api_key.dpapi"
    if (-not (Test-Path -LiteralPath $secretPath -PathType Leaf)) {
        throw "Gemini DPAPI secret is missing. Run scripts\configure_gemini.ps1 first."
    }

    Compose -ComposeArgs @("up", "-d", "secret-holder")

    $plain = $null
    try {
        $plain = Get-DpapiSecretValue -Path $secretPath -Label "Gemini API key"
        Write-SecretHolderFile -Value $plain -Path "/run/secret-store/mcp/gemini_api_key" -Label "Gemini API key"
    }
    finally {
        $plain = $null
    }

    & docker exec $SecretHolder test -s /run/secret-store/mcp/gemini_api_key
    if ($LASTEXITCODE -ne 0) { throw "Gemini key verification failed." }
    Write-Host "GEMINI_KEY_IMPORTED"
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

function Update-RuntimeSupervisorConfig([bool]$Enabled = $true) {
    if (-not (Test-Path -LiteralPath $SupervisorConfigPath -PathType Leaf)) {
        Write-Warning "Runtime supervisor config is unavailable; Docker-restart secret recovery is not registered."
        return
    }

    $supervisor = Get-Content -LiteralPath $SupervisorConfigPath -Raw | ConvertFrom-Json
    if ([int]$supervisor.version -ne 1) {
        throw "Unsupported runtime supervisor config version."
    }

    $holderRequired = @(
        "/run/secret-store/mcp/camofox_access_key",
        "/run/secret-store/mcp/camofox_admin_key",
        "/run/secret-store/camofox/access_key",
        "/run/secret-store/camofox/admin_key"
    )
    $mcpRequired = @(
        "/run/influencerresearch-secrets/camofox_access_key",
        "/run/influencerresearch-secrets/camofox_admin_key"
    )

    if (Test-Path -LiteralPath $InstagramDpapiPath -PathType Leaf) {
        $holderRequired += "/run/secret-store/mcp/instagram_cookies.json"
        $mcpRequired += "/run/influencerresearch-secrets/instagram_cookies.json"
    }

    $geminiPath = Join-Path $env:LOCALAPPDATA "InfluencerResearch\secrets\gemini_api_key.dpapi"
    if (Test-Path -LiteralPath $geminiPath -PathType Leaf) {
        $holderRequired += "/run/secret-store/mcp/gemini_api_key"
        $mcpRequired += "/run/influencerresearch-secrets/gemini_api_key"
    }

    $eventContainers = @(
        $SecretHolder,
        "influencerresearch-mcp",
        "influencerresearch-camofox"
    )
    $checks = @(
        [pscustomobject]@{
            container = $SecretHolder
            require_healthy = $false
            required_files = $holderRequired
        },
        [pscustomobject]@{
            container = "influencerresearch-mcp"
            require_healthy = $true
            required_files = $mcpRequired
        },
        [pscustomobject]@{
            container = "influencerresearch-camofox"
            require_healthy = $true
            required_files = @(
                "/run/camofox-secrets/access_key",
                "/run/camofox-secrets/admin_key"
            )
        }
    )

    if ($publicProxyConfigured) {
        $holderRequired += @(
            "/run/secret-store/proxy/access_key",
            "/run/secret-store/proxy/admin_key",
            "/run/secret-store/proxy/proxy_username",
            "/run/secret-store/proxy/proxy_password"
        )
        $eventContainers += "influencerresearch-camofox-public-proxy"
        $checks += [pscustomobject]@{
            container = "influencerresearch-camofox-public-proxy"
            require_healthy = $true
            required_files = @(
                "/run/camofox-proxy-secrets/access_key",
                "/run/camofox-proxy-secrets/admin_key",
                "/run/camofox-proxy-secrets/proxy_username",
                "/run/camofox-proxy-secrets/proxy_password"
            )
        }
        $checks[0].required_files = $holderRequired
    }

    $existing = @(
        $supervisor.runtimes |
            Where-Object { [string]$_.name -ne "influencerresearch" }
    )
    $runtimeEntry = [pscustomobject]@{
        name = "influencerresearch"
        enabled = $Enabled
        event_containers = $eventContainers
        health = [pscustomobject]@{
            checks = $checks
        }
        recovery = [pscustomobject]@{
            script = $RuntimeScriptPath
            arguments = @("-Action", "Recover")
            working_directory = $Repo
        }
        cooldown_seconds = 30
        recovery_wait_seconds = 90
    }

    $supervisor.runtimes = @($existing + $runtimeEntry)
    [IO.File]::WriteAllText(
        $SupervisorConfigPath,
        (($supervisor | ConvertTo-Json -Depth 10) + [Environment]::NewLine),
        [Text.UTF8Encoding]::new($false)
    )
}

switch ($Action) {
    "Up" {
        Invoke-ComposeUp
        Update-RuntimeSupervisorConfig -Enabled $true
        Write-Host "INFLUENCERRESEARCH_MCP=http://127.0.0.1:$($config.mcp_port)/mcp"
        Write-Host "Camofox is internal-only at http://camofox:9377"
        if ($publicProxyConfigured) { Write-Host "Public proxy fallback is internal-only at http://camofox-public-proxy:9377" }
    }
    "Redeploy" {
        Update-RuntimeSupervisorConfig -Enabled $false
        Invoke-ComposeUp
        Update-RuntimeSupervisorConfig -Enabled $true
        Write-Host "INFLUENCERRESEARCH_MCP=http://127.0.0.1:$($config.mcp_port)/mcp"
        Write-Host "Camofox is internal-only at http://camofox:9377"
        if ($publicProxyConfigured) { Write-Host "Public proxy fallback is internal-only at http://camofox-public-proxy:9377" }
    }
    "Recover" {
        Invoke-ComposeUp -Build $false
        Update-RuntimeSupervisorConfig -Enabled $true
    }
    "Down" {
        Update-RuntimeSupervisorConfig -Enabled $false
        Compose -ComposeArgs @("down")
    }
    "InstagramPublicSmoke" {
        Invoke-ComposeUp
        Update-RuntimeSupervisorConfig -Enabled $true
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
        Update-RuntimeSupervisorConfig -Enabled $true
        $tests = @(
            "test_camofox_container_config",
            "test_camofox_container_runtime",
            "test_camofox_public_proxy_fallback",
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
