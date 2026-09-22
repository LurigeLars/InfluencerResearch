# Camofox runtime

This directory defines the reviewed container runtime used by the TikTok/Camofox path.

- Direct dependency: `@askjo/camofox-browser` `1.13.1`
- Transitive browser client: `camoufox-js` `0.11.5`
- Container Node.js baseline: `v22.23.2`
- Camoufox browser baseline: `152.0.4` / `beta.28`

`package-lock.json` is the project-specific reviewed runtime baseline resolved from the exact direct dependency `@askjo/camofox-browser@1.13.1`. It is not a byte-for-byte copy of upstream's development lockfile.

The Docker build disables npm lifecycle scripts during dependency resolution, explicitly builds the required `better-sqlite3` native binding, and bakes the exact reviewed Linux Camoufox release into the image after verifying the release artifact. The dynamic Camofox postinstall browser fetch is not used.

Start or stop the service with `scripts/camofox_container.ps1`. The service publishes only `127.0.0.1:9377`, requires a locally generated access key, runs as the non-root `node` user with a read-only root filesystem and dropped Linux capabilities, disables crash reporting, and receives only one dedicated host transfer directory for browser downloads.

Browser profile, cookies, traces and cache are ephemeral inside the container. The host Python worker resets each temporary Camofox session through the authenticated persistence API. No normal host browser profile or home directory is mounted.

Any dependency, browser-baseline, filesystem mount, network exposure or authentication change requires re-review and a fresh isolated runtime test.
