# Camofox runtime

This directory defines the reviewed Camofox/Camoufox container used by the InfluencerResearch Docker runtime.

- Direct dependency: `@askjo/camofox-browser` `1.17.0`
- Transitive browser client: `camoufox-js` `0.11.5`
- Container Node.js baseline: `v22.23.2`
- Camoufox browser baseline: `152.0.4` / `beta.28`

`package-lock.json` is the project-specific reviewed runtime baseline resolved from the exact direct dependency `@askjo/camofox-browser@1.17.0`. It is not a byte-for-byte copy of upstream's development lockfile.

The Docker build disables npm lifecycle scripts during dependency resolution, explicitly builds the required `better-sqlite3` native binding, and bakes the exact reviewed Linux Camoufox release into the image after verifying the release artifact. The dynamic Camofox postinstall browser fetch is not used.

Camofox is a separate service in the root `compose.yaml`. It is reachable only from the Compose `runtime` network at `http://camofox:9377`; port 9377 is not published to the Windows host. The `influencerresearch` service authenticates to it with generated access/admin keys.

The service runs as the non-root `node` user with a read-only root filesystem, dropped Linux capabilities, no-new-privileges, explicit CPU/RAM/PID limits and no host bind mounts. Browser profile, cookies, traces and cache are ephemeral inside container tmpfs. Camofox is used for TikTok discovery/browser metadata only; individual TikTok media is downloaded by yt-dlp in the `influencerresearch` container.

Start, stop, inspect or smoke-test the complete stack with `scripts/runtime.ps1`.

Any dependency, browser-baseline, filesystem mount, network exposure or authentication change requires a fresh runtime test.
