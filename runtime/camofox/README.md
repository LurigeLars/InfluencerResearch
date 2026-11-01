# Camofox runtime

This directory defines the reviewed Camofox/Camoufox container used by the InfluencerResearch Docker runtime.

- Direct dependency: `@askjo/camofox-browser` `1.18.1` from reviewed fork commit `d4cf2b6b56c7d9fab2187f068d51e26eb9a8d2e6`
- Transitive browser client: `camoufox-js` `0.11.5`
- Tested Playwright protocol client: `1.63.0` (exact dependency in the reviewed fork; Playwright 1.64+ awaits upstream Juggler patch)
- Container Node.js baseline: `v26.13.0`
- Camoufox browser baseline: `158.0.1` / `beta.41` (official Linux AMD64 artifact, SHA256-pinned; Playwright 1.63.0 validated with fork PR #103)
- Reviewed fork adds bounded sanitized live observability via per-tab `/network` and `/console` routes; headers, cookies and request/response bodies are not exposed.
- Direct browser sessions also use the reviewed IPv6-aware, fail-closed SSRF guard; private/local destinations remain blocked by default.

`package-lock.json` is the project-specific reviewed runtime baseline resolved from the exact commit-pinned Camofox tarball above. The lockfile records the artifact integrity; it is not a byte-for-byte copy of upstream's development lockfile.

The Docker build disables npm lifecycle scripts during dependency resolution, explicitly builds the required `better-sqlite3` native binding, and bakes the exact reviewed Linux Camoufox release into the image after verifying the release artifact. The dynamic Camofox postinstall browser fetch is not used.

Direct Camofox is a separate service in the root `compose.yaml`. It is reachable only from the Compose `runtime` network at `http://camofox:9377`; port 9377 is not published to the Windows host. The `influencerresearch` service authenticates to it with generated access/admin keys.

An optional `public-proxy` Compose profile adds a second, isolated Camofox runtime at `http://camofox-public-proxy:9377`. Public TikTok/Instagram tab creation is always attempted on direct Camofox first. Only a target HTTP 403/429 on an allowlisted public host activates up to three bounded proxy attempts. The remainder of that browser session stays on the proxy runtime after takeover. Other hosts remain direct.

The proxy runtime reuses the DPAPI-protected Webshare username/password already configured for the local Firecrawl public-proxy fallback. The runtime appends Webshare's `-rotate` username parameter and injects the credentials only into proxy-Camofox lifecycle tmpfs. Direct Camofox and authenticated Instagram Playwright ingestion never receive those proxy credentials or copy target cookies/auth state into the proxy browser.

Both browser services run as the non-root `node` user with read-only root filesystems, dropped Linux capabilities, no-new-privileges, explicit CPU/RAM/PID limits and no host bind mounts. Browser profiles, cookies, traces and caches are ephemeral inside container tmpfs. Proxy-Camofox points `CAMOUFOX_INSTALL_DIR` at writable tmpfs while keeping the reviewed browser executable on read-only `/opt/camoufox`. Its GeoIP setup timeout is set to 30 seconds in IR; the reviewed fork keeps the 10-second default and enforces a 30-second global ceiling, covering the observed cold GeoLite download without permitting multi-minute browser-launch stalls. At startup it builds a minimal install shim by symlinking the read-only `version.json` and `fontconfig/` metadata into that tmpfs directory; `camoufox-js` can then read its required browser metadata and write only the GeoIP database beside those links without weakening the root filesystem. Individual TikTok media remains yt-dlp's responsibility in the `influencerresearch` container; this fallback covers browser discovery only. Aggregate fallback counters are persisted without URLs, content, cookies or credentials.

Start, stop, inspect or smoke-test the complete stack with `scripts/runtime.ps1`.

Any dependency, browser-baseline, filesystem mount, network exposure or authentication change requires a fresh runtime test.

The reviewed source is pinned to a full fork commit SHA rather than the package semver because this fork uses the same `1.18.1` package version for security and source changes. A source-promotion PR must update both manifests, the locked transitive dependency graph, the provenance assertion and the security smoke tests together; changing `master` alone never changes running IR containers.

Browser release promotion is separate from the Camofox server source promotion. `.github/workflows/review-approved-camoufox-browser.yml` checks the browser version and x86_64 SHA256 **already merged** into `LurigeLars/camofox-browser/master`, confirms the downloader and official upstream release digest, and proposes a **draft IR PR** only when IR's independent browser pin differs. The PR must pass IR's runtime tests and `camoufox-ir-image-smoke.yml` (a real Docker build that downloads and verifies the browser ZIP), plus security analysis and explicit compatibility review. New unmerged upstream beta releases are not promoted. The review workflow never updates running images, secrets, auth, proxy configuration or Instagram ingestion.
