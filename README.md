# InfluencerResearch

InfluencerResearch is a standalone research-ingestion and evaluation toolkit for collecting publicly available creator content from supported platforms, transcribing media, and building structured research queues.

It is **not a fork** of yt-dlp or Camofox. The project orchestrates those tools as external dependencies alongside Playwright, faster-whisper and other libraries.

## Runtime

The current Windows-oriented setup expects:

- CPython 3.12 x64
- Node.js `v22.23.2` for the current reviewed Camofox baseline
- a dedicated browser profile for authenticated Instagram access where required

Install the Python runtime with `01_install.bat`. Install the pinned Camofox runtime used by the TikTok/browser flow with:

```powershell
pwsh -NoProfile -File scripts\install_camofox.ps1
```

The Camofox dependency tree is tracked in `runtime/camofox/package.json` and `runtime/camofox/package-lock.json`. The installer uses that lockfile and verifies the exact Node, package and browser baseline expected by the runtime.

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
- [Camofox Browser](https://github.com/jo-inc/camofox-browser) — browser automation used by the TikTok/browser fallback path
- [Playwright for Python](https://github.com/microsoft/playwright-python) — browser automation
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — transcription
- [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) — FFmpeg integration

Pinned versions are defined in `requirements.txt`, `requirements_tiktok_impersonation.lock.txt`, and `runtime/camofox/package-lock.json`.

## Project license

No project-wide open-source license has been selected yet. Until a `LICENSE` file is added, normal copyright restrictions apply to this repository's own source code. Third-party dependencies retain their respective licenses.
