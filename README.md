# InfluencerResearch

[![Runtime regression](https://github.com/LurigeLars/InfluencerResearch/actions/workflows/runtime-regression.yml/badge.svg)](https://github.com/LurigeLars/InfluencerResearch/actions/workflows/runtime-regression.yml)
[![CodeQL](https://github.com/LurigeLars/InfluencerResearch/actions/workflows/codeql.yml/badge.svg)](https://github.com/LurigeLars/InfluencerResearch/actions/workflows/codeql.yml)
[![Static analysis](https://github.com/LurigeLars/InfluencerResearch/actions/workflows/static-analysis.yml/badge.svg)](https://github.com/LurigeLars/InfluencerResearch/actions/workflows/static-analysis.yml)
![Runtime](https://img.shields.io/badge/runtime-Python%203.14-blue)

A bounded research-ingestion system for studying **public creator content** across
supported platforms.

InfluencerResearch discovers creator content, collects evidence, transcribes and reviews
media, persists research state and exposes the workflow through a deliberately small MCP
surface. Long-running work is handled as explicit jobs instead of trying to keep one MCP
request open until an entire research run finishes.

It is designed as research infrastructure, not as a generic browser, social-media bot or
arbitrary command-execution service.

## Why this project exists

Researching creators across YouTube, TikTok and Instagram is not one API call. A useful
workflow has to deal with:

- creator identity across platforms and changing handles;
- recent-content discovery over a defined time window;
- browser-dependent public pages and anti-bot friction;
- video/audio acquisition and transcription;
- visual evidence when spoken text is not enough;
- Stories and other ephemeral content;
- retries, partial coverage and evidence provenance;
- long-running jobs that must survive beyond a single chat turn;
- a persistent registry so the next research run knows what has already been seen.

This repository turns those concerns into a repeatable pipeline with explicit state and
coverage semantics.

## What it does

At a high level the system can:

- register and inspect creator identities;
- evaluate a creator from collected public evidence;
- monitor registered creators;
- discover and ingest recent public content;
- collect YouTube, TikTok and Instagram evidence through platform-specific paths;
- transcribe audio/video with Gemini or local faster-whisper;
- extract selected visual evidence, including bounded OCR/review paths;
- preserve analysis evidence and research artifacts;
- expose job status/cancellation to MCP clients.

The current MCP surface is intentionally limited to seventeen tools:

- `creator_list`
- `creator_get`
- `creator_register`
- `creator_update`
- `creator_retire`
- `creator_evaluate`
- `creator_monitor`
- `creator_recent_check`
- `analysis_queue_list`
- `analysis_queue_get`
- `analysis_queue_mark_insufficient`
- `analysis_decision_list`
- `analysis_decision_record`
- `analysis_decision_record_batch`
- `analysis_evidence_get`
- `research_status`
- `research_stop`

There is no generic shell/command tool.

## Job model

Some research operations take much longer than a normal interactive MCP call. They are
therefore submitted as allowlisted jobs and tracked through durable research state.

```text
MCP client
   |
   | start research
   v
InfluencerResearch MCP
   |
   v
research job/state
   |
   +--> discovery
   +--> ingestion/download
   +--> transcription
   +--> visual evidence
   +--> persistence
   |
   v
research_status / analysis_evidence_get
```

Only one long-running research job runs at a time. That serialization is intentional:
browser/media pipelines are expensive and shared state should not be corrupted by
competing research runs.

## Runtime architecture

The maintained deployment is Docker-based:

```text
                     local host
                        |
                        v
             InfluencerResearch MCP
                  Python 3.14
                        |
          +-------------+-------------+
          |             |             |
          v             v             v
     research state  media/tools   Camofox
                         |         browser service
                         |
              transcription / OCR
```

Camofox is an internal browser dependency, not a public browser product. It is used for
supported discovery flows where a real browser is required. The repository also uses
Playwright and yt-dlp in platform-specific ingestion paths.

For remote MCP access:

```text
ChatGPT / remote MCP client
      |
      v
Cloudflare Access
      |
      v
reviewed gateway
      |
      v
InfluencerResearch MCP
```

Camofox itself remains internal and has no public MCP endpoint.

## Security and privacy model

The project is intentionally narrower than the browser/media components underneath it.

- The MCP runtime, Camofox service and public gateway run non-root.
- The public gateway exposes the same fixed seventeen-tool allowlist as the MCP server.
- Long-running research accepts fixed research operations rather than arbitrary commands.
- Instagram session material, Gemini credentials and Camofox service keys are protected
  on the Windows host with DPAPI and injected into tmpfs-backed runtime secret volumes.
- Browser state and generated research data stay outside the public repository.
- Machine-specific paths, creator datasets, identities, Cloudflare values and credentials
  must not be committed.
- Optional public-proxy fallback is limited to allowlisted public hosts and does not carry
  authenticated Instagram sessions or TikTok media downloads.
- Public content and page text are evidence/data, not instructions to the runtime.

Use only accounts, content and automation flows you are authorized to access, and comply
with applicable law and platform terms.

## What this project is not

- It is not a generic remote browser.
- It is not an account-management or posting bot.
- It does not expose arbitrary command execution.
- It is not a fork of yt-dlp, Camofox/Camoufox, Playwright or their upstream projects.
- It does not put browser profiles, creator research output or credentials in Git.
- A `PARTIAL` research result is not silently treated as complete coverage.

## Repository relationship to Camofox

InfluencerResearch uses Camofox/Camoufox as an **internal browser runtime** for selected
public discovery workflows. The browser dependency is built separately and accessed over
the private Compose network.

This repository owns the research semantics: creator identity, coverage windows,
ingestion, evidence, retries, persistence and MCP job behavior. Camofox owns browser
execution. Keeping that boundary explicit makes it possible to update or patch the
browser implementation without turning browser internals into the public research API.

## Quick start

The canonical runtime is Docker Compose and is managed through the PowerShell runtime
script:

```powershell
pwsh -NoProfile -File scripts\runtime.ps1 -Action Up
pwsh -NoProfile -File scripts\runtime.ps1 -Action Status
pwsh -NoProfile -File scripts\runtime.ps1 -Action Smoke
```

The MCP server is published only on loopback. The default host endpoint is:

```text
http://127.0.0.1:8770/mcp
```

The host port is configurable. Check the actual mapping before configuring a local MCP
client.

The rest of this README documents the runtime, authentication bootstrap, Camofox,
transcription and public gateway in detail.

## Detailed runtime deployment

The canonical runtime is Docker Compose with a networkless secret-holder, two always-on application services, and one optional public-proxy browser service:

- `secret-holder` — networkless non-root helper that receives DPAPI-decrypted values from the host and writes them into tmpfs-backed Docker volumes.
- `influencerresearch` — Python 3.14 workers plus the official MCP Python SDK. Streamable HTTP MCP is published to the Windows host only at `http://127.0.0.1:8770/mcp`.
- `camofox` — direct isolated Camofox/Camoufox browser service reachable only inside Compose at `http://camofox:9377`.
- `camofox-public-proxy` — optional internal-only Camofox service enabled by the `public-proxy` profile when the existing Firecrawl Webshare DPAPI credentials are available.

Neither Camofox service has a host-published port or MCP surface. Public TikTok/Instagram browser discovery goes direct first; only target HTTP 403/429 on allowlisted hosts triggers up to three bounded proxy attempts. Successful takeover keeps that browser session on the proxy runtime. Authenticated Instagram Playwright ingestion and TikTok media downloads do not use this browser proxy fallback.

Persistent research data uses narrow bind mounts for the existing parent-root `control/`, `state/`, `output/` and `logs/` directories. Browser cache uses the `influencerresearch-runtime` Docker volume. Camofox service keys, the Instagram portable session export and the Gemini API key are protected on the Windows host with DPAPI and materialized only into tmpfs-backed secret volumes through the networkless secret-holder. Consumer containers mount only the secret volume they need, read-only. The optional browser fallback reuses the DPAPI-protected Webshare username/password from `%LOCALAPPDATA%\FirecrawlLocal\secrets`; those credentials are exposed only to proxy-Camofox and are never exposed to direct Camofox.

The runtime expects the host control directory one level above the repository (for example `<redacted-workspace>\control`). On a fresh installation, initialize the required settings file from the tracked non-secret template:

```powershell
Copy-Item .\control\settings.example.json ..\control\settings.json
```

Keep `..\control\settings.json` as local runtime configuration; do not commit machine-specific control data.

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

Run the login-free Instagram public-profile smoke:

```powershell
pwsh -NoProfile -File scripts\runtime.ps1 -Action InstagramPublicSmoke
```

The smoke also probes the creator's public Story URL without login and reports whether Story access is public, requires authentication, has no active Story, or is inconclusive. Override the profile, handle or run count with `-InstagramProfileUrl`, `-InstagramHandle` and `-InstagramRuns`.

Stop the stack:

```powershell
pwsh -NoProfile -File scripts\runtime.ps1 -Action Down
```

The MCP v1 tool surface is deliberately small:

- `creator_list`
- `creator_get`
- `creator_register`
- `creator_update`
- `creator_retire`
- `creator_evaluate`
- `creator_monitor`
- `creator_recent_check`
- `creator_evaluation_item_list`
- `analysis_queue_list`
- `analysis_queue_get`
- `analysis_queue_mark_insufficient`
- `analysis_decision_list`
- `analysis_decision_record`
- `analysis_decision_record_batch`
- `analysis_evidence_get`
- `research_status`
- `research_stop`

Long-running research operations are fixed allowlisted jobs. Only one research job may run at a time. There is no generic command-execution tool.

`creator_register` also accepts optional `supersedes_creator_keys`. This is an explicit registry operation for disabling known duplicate/legacy creator keys after consolidation. Identity is not inferred from matching handles, display names, or platform URLs; aliases may differ across platforms. Superseded profiles are retained as `DISABLED` with `superseded_by` metadata so historical research remains addressable.

`creator_update` performs a partial update of an existing ACTIVE creator. Each source patch names a `platform` and may change only `enabled`, `evaluation_enabled`, `monitoring_enabled`, and `priority`; omitted fields are preserved. Creator-level `monitoring_enabled` is derived as `any(enabled source with monitoring_enabled=true)` and is always false for non-ACTIVE creators. Updates do not start ingestion or evaluation jobs.

`creator_retire` is a soft-delete operation. It changes an ACTIVE creator to `RETIRED`, disables all source-level `enabled`, `evaluation_enabled`, and `monitoring_enabled` flags, and records retirement metadata. Historical research/evidence is not deleted. `creator_get` can still retrieve retired records, while `creator_list()` hides them by default; pass `include_retired=true` to include ACTIVE and RETIRED creators. Repeating the same retirement is an idempotent no-op.

`creator_evaluate` treats registered YouTube `evaluation_video_ids` as must-include seeds. They are evaluated first, then channel discovery fills the remaining requested sample with unique eligible items. Unpinned YouTube candidates are evidence-cost preflighted before local Whisper: by default, a candidate longer than 20 minutes must have a usable English/Swedish caption transcript or it is deferred and a later discovered item backfills the sample. Streams remain eligible, and pinned/exact IDs deliberately bypass this cost defer. The duration ceiling can be changed with `INFLUENCER_RESEARCH_YOUTUBE_MAX_UNPINNED_WHISPER_DURATION_SECONDS` (bounded to 5–60 minutes). Deferred items and reasons are exposed in evaluation status. Each completed YouTube item is reconciled into the canonical analysis queue immediately, with a terminal JobManager reconciliation safety net for STOPPED, PARTIAL, FAILED, watchdog and orphan-recovery paths. Selection persists explicit run membership before expensive evidence work starts, so already-complete items can participate in a later evaluation without overwriting their original manifest `evaluation_run_id`; terminal reconciliation follows that membership rather than inferring it only from provenance. `creator_evaluation_item_list` is a read-only canonical join over manifest, analysis queue and finalized decisions, so every evaluated item can be inspected by run/creator/platform and classified as pending analysis, finalized, duplicate, insufficient, not analysis-ready, pipeline-incomplete, queue-missing or unaccounted. Terminal reconciliation also persists explicit `analysis_*` disposition counters into `research_status`, including any unaccounted queue IDs. YouTube visual evidence capture now writes FFmpeg diagnostics to disk instead of buffering them in model-process memory, emits progress heartbeats during frame capture and OCR/bundle postprocessing, and stops the yt-dlp/FFmpeg child processes if their combined process-tree RSS exceeds 768 MiB by default. Override that ceiling with `INFLUENCER_RESEARCH_YOUTUBE_VISUAL_CAPTURE_MAX_CHILD_RSS_MB` (bounded to 256–1024 MiB). The IR runtime image also includes pinned Node 26.10.0, which `_node_runtime_arg()` supplies explicitly to yt-dlp for YouTube EJS extraction. `research_status` exposes evaluation phase/heartbeat and sample coverage. A creator evaluation that stops making progress is terminalized as `FAILED / NO_PROGRESS_TIMEOUT` rather than remaining `RUNNING` indefinitely.

`analysis_queue_list` and `analysis_queue_get` provide the current canonical analysis worklist without direct state-file access. Retired creators are excluded from the pending list, while specific historical queue/decision records remain readable. `analysis_queue_mark_insufficient` is the non-decision path for retained evidence that proves unusable after direct inspection: it validates the evidence lineage, removes the item from pending analysis, records `INSUFFICIENT_CONTENT` in the manifest, preserves provenance/evidence and is idempotent. It does not create an `IGNORE` decision and therefore does not distort investment-decision or creator-yield statistics. `analysis_decision_list` is a read-only view over canonical `research_decisions.json` joined to manifest metadata, with creator/platform/decision/evaluation-run/date filters and pagination. `analysis_decision_record` and `analysis_decision_record_batch` validate the existing canonical decision schema, persist to `research_decisions.json`, apply the existing manifest/follow-up governance, and remove finalized work from the active pending queue. Identical replay is idempotent; a conflicting second decision is rejected instead of silently overwriting the ledger.

The former Windows request-file/Scheduled-Task research bridge is retired and is not part of the canonical runtime.

## Connecting a local client

The MCP surface is streamable HTTP on loopback only. Local agents connect to it directly; cloud
chats reach it through the Cloudflare gateway described below, and nothing else is exposed.

| Client | Connection |
|---|---|
| Claude Code, Codex | loopback HTTP |
| Claude Desktop, Cursor | a stdio bridge to the same loopback endpoint |
| ChatGPT and other cloud chats | Cloudflare Access to the public gateway |

**The host port is configurable and worth checking before you assume it.** `compose.yaml` publishes
`127.0.0.1:${INFLUENCER_RESEARCH_MCP_PORT:-8770}:8770`, so the container always listens on 8770
internally while the host port follows that variable. Set it when another local service already
holds 8770, which is easy to do when several MCP services run on one machine. Confirm the port in
use before configuring a client:

```powershell
docker ps --filter name=influencerresearch-mcp --format "{{.Ports}}"
```

**Claude Code and Codex**, substituting the port you found:

```bash
claude mcp add --transport http influencerresearch http://127.0.0.1:<port>/mcp
codex mcp add influencerresearch --url http://127.0.0.1:<port>/mcp
```

**Claude Desktop and Cursor** read stdio commands from their configuration files rather than URLs,
so an HTTP-only server needs a small stdio bridge that forwards to the loopback endpoint. That is
the same `local-mcp` proxy pattern the other local MCP services in this fleet use.

No credential is needed on loopback: the authentication boundary is Cloudflare Access on the public
path, not the local one. Keep the endpoint bound to `127.0.0.1` so that stays true.

**Verify** by listing the tools. The local surface is eighteen: `creator_register`, `creator_update`, `creator_retire`, `creator_list`,
`creator_get`, `creator_evaluate`, `creator_monitor`, `creator_recent_check`, `creator_evaluation_item_list`, `analysis_queue_list`,
`analysis_queue_get`, `analysis_queue_mark_insufficient`, `analysis_decision_list`, `analysis_decision_record`,
`analysis_decision_record_batch`, `analysis_evidence_get`, `research_status` and `research_stop`.
The public allowlist is the same eighteen.

## Public Cloudflare access

The optional public stack follows the same pattern as the other local MCP services:

```text
Cloudflare Access
  -> shared Cloudflare Tunnel
  -> influencer-gateway:8080
  -> influencerresearch:8770/mcp
```

The gateway publishes no host ports. It joins the existing `influencerresearch_runtime` Docker network and the shared tunnel's edge network, forwarding only to the internal MCP service. Camofox remains inaccessible from the public stack.

Real deployment identifiers are intentionally not stored in this repository. `public/gateway.env.example` contains placeholders only.
Copy it to `public/gateway.env`, replace the placeholders with your own deployment values, and keep the real file gitignored.
Configure the public hostname, connector endpoint and Access application for your own deployment. The shared tunnel is managed outside this repository.

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

Cloudflare Access remains the authentication boundary. The gateway independently verifies the Access JWT audience/issuer, restricts requests to `/mcp`, caps request size/rate, strips client credentials before proxying, and applies the same seventeen-tool public allowlist as the MCP server.

## Instagram authentication bootstrap

Instagram authentication requires an occasional interactive browser login. This is a **bootstrap step only**, not a continuously running local service.

Copy `.env.example` to `.env`, replace the placeholder with your own account setting, and keep the real file gitignored.

Run the bootstrap:

```powershell
pwsh -NoProfile -File scripts\authenticate_instagram.ps1
```

The script opens a dedicated local Chrome profile, verifies the Instagram session, exports the cookies only to a short-lived local temp file, then protects the JSON with Windows DPAPI CurrentUser at:

```text
%LOCALAPPDATA%\InfluencerResearch\secrets\instagram_cookies.dpapi
```

The temporary plaintext export is removed after a successful DPAPI round-trip verification. Existing installations with the old `instagram_cookies.json` format are migrated on the next import/start; the plaintext file is deleted only after the DPAPI value decrypts successfully and matches the old payload.

When the Docker runtime is already running, import/refresh the session with:

```powershell
pwsh -NoProfile -File scripts\runtime.ps1 -Action ImportInstagramAuth
```

On `-Action Up`, the runtime decrypts the DPAPI blob in memory and streams the cookie JSON through the networkless secret-holder into a tmpfs-backed secret volume mounted read-only at `/run/influencerresearch-secrets/instagram_cookies.json`. The session is absent from Docker container metadata and persistent disk. It survives recreation of the consumer container while the secret-holder volume remains mounted; after a Docker/tmpfs reset the runtime supervisor rehydrates it from DPAPI.

## Camofox service secrets

The internal Camofox access/admin keys are generated locally and protected with Windows DPAPI. They are not stored in the repository, Compose environment, Docker container metadata, or the persistent runtime volume.

The encrypted host blobs live at:

```text
%LOCALAPPDATA%\InfluencerResearch\secrets\camofox_access_key.dpapi
%LOCALAPPDATA%\InfluencerResearch\secrets\camofox_admin_key.dpapi
```

On `runtime.ps1 -Action Up`, the host decrypts them in memory and streams them into isolated tmpfs-backed secret volumes through the networkless secret-holder. The Python and Camofox services receive read-only mounts, and the Camofox process exports the values only inside its own process immediately before starting the reviewed server. The registered runtime-supervisor recovery path can rehydrate these volumes after a Docker restart without placing the plaintext values in Compose environment or container metadata.

Existing installations are migrated automatically: a schema-v1 `docker-runtime.json` containing `camofox_access_key` / `camofox_admin_key` is copied into DPAPI-backed blobs, verified, and rewritten as schema v2 containing only non-secret runtime configuration such as the MCP port.

## Gemini transcription secret

Gemini transcription uses a Windows-hosted DPAPI secret and a runtime-only tmpfs file. The API key is never stored in the repository, a Compose environment file, container metadata, or the persistent Docker runtime volume.

Store or rotate the key once on the Windows host:

```powershell
pwsh -NoProfile -File scripts\configure_gemini.ps1
```

The encrypted DPAPI blob is written to:

```text
%LOCALAPPDATA%\InfluencerResearch\secrets\gemini_api_key.dpapi
```

It is bound to the current Windows user by DPAPI. On `runtime.ps1 -Action Up`, the host decrypts the key in memory and streams it through the secret-holder into the tmpfs-backed secret volume mounted read-only at:

```text
/run/influencerresearch-secrets/gemini_api_key
```

That path is backed by tmpfs and remains available across recreation of the consumer container while the secret-holder volume stays mounted. After a Docker/tmpfs reset the runtime supervisor rehydrates it from DPAPI. To refresh it after rotating the key:

```powershell
pwsh -NoProfile -File scripts\runtime.ps1 -Action ImportGeminiKey
```

The shared transcription backend supports `auto`, `gemini`, and `faster-whisper`. With the default `auto` provider, Gemini is preferred when the runtime secret is present and `faster-whisper` is the local fallback. Example `control/settings.json` transcription section:

```json
{
  "transcription": {
    "enabled": true,
    "provider": "auto",
    "gemini_model": "gemini-3.5-transcribe",
    "model_size": "small",
    "device": "cpu",
    "compute_type": "int8",
    "beam_size": 5,
    "vad_filter": true
  }
}
```

The backend never reads `GEMINI_API_KEY` from process environment variables.

### One-time local namespace migration

Older installations stored host-only auth/fallback state under `%LOCALAPPDATA%\InstagramResearch`. The canonical host namespace is now `%LOCALAPPDATA%\InfluencerResearch`.

Preview the migration:

```powershell
pwsh -NoProfile -File scripts\migrate_local_namespace.ps1 -Action Plan
```

Apply and verify it:

```powershell
pwsh -NoProfile -File scripts\migrate_local_namespace.ps1 -Action Apply
pwsh -NoProfile -File scripts\migrate_local_namespace.ps1 -Action Verify
```

The migration refuses to run while the retired Windows request bridge still exists, never overwrites an existing active destination, deletes only explicitly retired bridge artifacts, and preserves otherwise-unclassified historical files under `%LOCALAPPDATA%\InfluencerResearch\legacy-archive-2026-09-26`.

## Camofox baseline

The Camofox dependency tree is tracked in `runtime/camofox/package.json` and `runtime/camofox/package-lock.json`.

The reviewed container baseline is:

- Camofox Browser `1.18.1`
- `camoufox-js` `0.11.5`
- Node.js `26.10.0`
- Camoufox `152.0.4` / `beta.30`

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
