# Camofox runtime manifest

This directory defines the reviewed Node.js dependency tree used by the TikTok/Camofox path.

- Direct dependency: `@askjo/camofox-browser` `1.13.1`
- Transitive browser client: `camoufox-js` `0.11.5`
- Required Node.js baseline: `v22.23.2`
- Reviewed Camoufox browser baseline: `152.0.4` / `beta.28`

`package-lock.json` is the project-specific reviewed runtime baseline resolved from the exact direct dependency `@askjo/camofox-browser@1.13.1`. It is not a byte-for-byte copy of upstream's development lockfile. Installation omits development and optional packages.

Install with `scripts/install_camofox.ps1`. The installer builds and verifies the complete runtime in a staging directory first. Lifecycle scripts are disabled during `npm ci`; only the required `better-sqlite3` rebuild and reviewed Camofox Browser postinstall are then executed. Common credential environment variables are cleared while those external scripts run. The existing runtime is replaced only after staging passes all version/browser checks, with rollback attempted if activation fails.

Any dependency or browser-baseline update must update this lockfile, the accepted provenance metadata in `tiktok_camofox_sync.py`, and the associated tests in the same change.
