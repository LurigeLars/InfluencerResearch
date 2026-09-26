# InfluencerResearch

InfluencerResearch is a standalone research-ingestion and evaluation toolkit for collecting publicly available creator content from supported platforms, transcribing media, and building structured research queues.

It is **not a fork** of yt-dlp or Camofox. The project orchestrates those tools as external dependencies alongside the official Python MCP SDK, Playwright, faster-whisper and other libraries.

## Runtime architecture

The canonical runtime is Docker Compose with two services on the same private `runtime` network:

- `influencerresearch` — Python 3.12 workers plus the official MCP Python SDK. Streamable HTTP MCP is published to the Windows host only at `http://127.0.0.1:8770/mcp`.
- `camofox` — isolated Camofox/Camoufox browser service reachable only inside Compose at `http://camofox:9377`.

Camofox has **no host-published port** and is not an MCP surface. TikTok browser discovery/metadata uses Camofox; individual media downloads remain yt-dlp's responsibility inside the `influencerresearch` container.

Persistent research data uses narrow bind mounts for the existing parent-root `control/`, `state/`, `output/` and `logs/` directories. Browser/runtime secrets and cache live in the `influencerresearch-runtime` Docker volume.

Start the complete stack:

```powershell
pwsh -NoProfile -File scripts\runtime.ps1 -Action Up
```

Check status:

```powershell
pwsh -NoProfile -File scripts\runtime.ps1 -Action Status
```

Run the container/runtime smoke:

```powershell
pwsh -NoProfile -File scripts\runtime.ps1 -Action Smoke
```

Stop the stack:

```powershell
pwsh -NoProfile -File scripts\runtime.ps1 -Action Down
```

The MCP v1 tool surface is deliberately small:

- `creator_list`
- `creator_get`
- `creator_register`
- `creator_evaluate`
- `creator_monitor`
- `creator_recent_check`
- `research_status`
- `research_stop`

Long-running research operations are fixed allowlisted jobs. Only one research job may run at a time. There is no generic command-execution tool.

The former Windows request-file/Scheduled-Task research bridge is retired and is not part of the canonical runtime.

## Public Cloudflare access

The optional public stack follows the same pattern as the other local MCP services:

```text
Cloudflare Access
  -> remotely managed Cloudflare Tunnel
  -> gateway:8080
  -> influencerresearch:8770/mcp
```

The gateway and `cloudflared` containers publish no host ports. The gateway joins the existing `influencerresearch_runtime` Docker network and forwards only to the internal MCP service. Camofox remains inaccessible from the public stack.

Cloudflare should be configured with:

- public hostname: `influencer.lurigelars.com`
- tunnel origin service: `http://gateway:8080`
- connector endpoint: `https://influencer.lurigelars.com/mcp`
- Cloudflare Access application protecting the MCP hostname/path

Prepare local configuration:

```powershell
Copy-Item public\gateway.env.example public\gateway.env
Copy-Item public\tunnel.env.example public\tunnel.env
```

Fill `public/gateway.env` with the Access team domain and Application Audience (AUD) tag. Optionally set an email allowlist for interactive Access identity. Fill `public/tunnel.env` with the token from the remotely managed Cloudflare Tunnel. Both local files are gitignored and dockerignored.

Start the base runtime first, then the public edge:

```powershell
pwsh -NoProfile -File scripts\runtime.ps1 -Action Up
pwsh -NoProfile -File scripts\public.ps1 -Action Up
```

Inspect without exposing credentials:

```powershell
pwsh -NoProfile -File scripts\public.ps1 -Action Status
pwsh -NoProfile -File scripts\public.ps1 -Action Logs
```

Cloudflare Access remains the authentication boundary. The gateway independently verifies the Access JWT audience/issuer, restricts requests to `/mcp`, caps request size/rate, strips client credentials before proxying, and applies the same eight-tool public allowlist as the MCP server.

## Instagram authentication bootstrap

Instagram authentication requires an occasional interactive browser login. This is a **bootstrap step only**, not a continuously running local service.

Set the dedicated account username:

```powershell
$env:INFLUENCER_RESEARCH_INSTAGRAM_USERNAME="your_instagram_username"
```

Run the bootstrap:

```powershell
pwsh -NoProfile -File scripts\authenticate_instagram.ps1
```

The script opens a dedicated local Chrome profile, verifies the Instagram session, and exports only the session cookies to `%LOCALAPPDATA%\InfluencerResearch\secrets\instagram_cookies.json`. The cookie export is sensitive and must never be committed or copied to Drive.

When the Docker runtime is already running, import/refresh the session with:

```powershell
pwsh -NoProfile -File scripts\runtime.ps1 -Action ImportInstagramAuth
```

On `-Action Up`, the runtime automatically imports the local cookie export when it exists. Instagram workers then run headless inside the `influencerresearch` container using the persistent Docker runtime volume.

## Camofox baseline

The Camofox dependency tree is tracked in `runtime/camofox/package.json` and `runtime/camofox/package-lock.json`.

The reviewed container baseline is:

- Camofox Browser `1.17.0`
- `camoufox-js` `0.11.5`
- Node.js `22.23.2`
- Camoufox `152.0.4` / `beta.28`

The Camofox image runs non-root with a read-only root filesystem, dropped capabilities, no-new-privileges, bounded CPU/RAM/PIDs and ephemeral browser state. It has no host bind mounts.

## Security and privacy

Do not commit credentials, cookies, browser profiles, generated research data, logs or local environment files. The repository's `.gitignore` excludes the standard local runtime locations, but that is not a substitute for reviewing staged changes before pushing.

Use only accounts, content and automation flows you are authorized to access, and comply with applicable law and platform terms.

## Key external components

- [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk) — typed Streamable HTTP MCP server
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — media extraction/downloading
- [Camofox Browser](https://github.com/jo-inc/camofox-browser) — isolated TikTok browser discovery/metadata
- [Playwright for Python](https://github.com/microsoft/playwright-python) — Instagram browser automation
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — transcription
- [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) — FFmpeg integration

Pinned versions are defined in `requirements.txt`, `requirements_tiktok_impersonation.lock.txt` and `runtime/camofox/package-lock.json`.

The repository intentionally contains no legacy `.bat` entrypoints.

## Project license

No project-wide open-source license has been selected yet. Until a `LICENSE` file is added, normal copyright restrictions apply to this repository's own source code. Third-party dependencies retain their respective licenses.
