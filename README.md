# InfluencerResearch

InfluencerResearch is a standalone research-ingestion and evaluation toolkit for collecting publicly available creator content from supported platforms, transcribing media, and building structured research queues.

It is **not a fork** of yt-dlp or Camofox. The project orchestrates those tools as external dependencies alongside Playwright, faster-whisper and other libraries.

## Runtime

The host-side Python setup currently expects:

- CPython 3.12 x64
- Docker Desktop for the isolated Camofox browser service
- a dedicated browser profile for authenticated Instagram access where required

Install the Python runtime with `01_install.bat`. Build and start the pinned Camofox service used by the TikTok/browser flow with:

```powershell
pwsh -NoProfile -File scripts\camofox_container.ps1 -Action Up
```

Camofox is bound only to host loopback on `127.0.0.1:9377`. Its local access/admin keys live under `%LOCALAPPDATA%\InfluencerResearch` and are not stored in the repository. The container has no host bind mount and no access to the normal Windows user profile, browser profiles, Drive folders, or unrelated project data. TikTok profile discovery/browser metadata uses Camofox; individual TikTok media is downloaded by yt-dlp.

The Camofox dependency tree is tracked in `runtime/camofox/package.json` and `runtime/camofox/package-lock.json`. The Docker image uses the reviewed Camofox Browser `1.13.1`, `camoufox-js` `0.11.5`, Node `22.23.2`, and Camoufox `152.0.4` / `beta.28` baseline.

For Instagram authentication, set:

```
INFLUENCER_RESEARCH_INSTAGRAM_USERNAME=your_instagram_username
```

Then run `02_authenticate.bat`. Authentication state is stored locally under the legacy compatibility runtime directory `%LOCALAPPDATA%\InstagramResearch` and must never be committed.

## Security and privacy

Do not commit credentials, cookies, browser profiles, generated research data, logs, or local environment files. The repository's `.gitignore` excludes the standard local runtime locations, but that is not a substitute for reviewing staged changes before pushing.

Use only accounts, content and automation flows you are authorized to access, and comply with applicable law and platform terms.

## Key external components

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — media extraction/downloading
- [Camofox Browser](https://github.com/jo-inc/camofox-browser) — isolated browser automation for TikTok profile discovery and browser metadata fallback
- [Playwright for Python](https://github.com/microsoft/playwright-python) — browser automation
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — transcription
- [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) — FFmpeg integration

Pinned versions are defined in `requirements.txt`, `requirements_tiktok_impersonation.lock.txt`, and `runtime/camofox/package-lock.json`.

## Project license

No project-wide open-source license has been selected yet. Until a `LICENSE` file is added, normal copyright restrictions apply to this repository's own source code. Third-party dependencies retain their respective licenses.
