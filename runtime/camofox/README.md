# Camofox runtime manifest

This directory defines the reviewed Node.js dependency tree used by the TikTok/Camofox path.

- Direct dependency: `@askjo/camofox-browser` `1.13.1`
- Transitive browser client: `camoufox-js` `0.11.5`
- Required Node.js baseline: `v22.23.2`
- Reviewed Camoufox browser baseline: `152.0.4` / `beta.28`

`package-lock.json` is the project-specific reviewed runtime baseline resolved from the exact direct dependency `@askjo/camofox-browser@1.13.1`. It is not a byte-for-byte copy of upstream's development lockfile. Installation omits development and optional packages.

Install with `scripts/install_camofox.ps1`. The installer first performs `npm ci` with lifecycle scripts disabled, then runs only the required `better-sqlite3` rebuild and the reviewed Camofox Browser postinstall. It clears common credential environment variables while those external scripts execute.

Any dependency or browser-baseline update must update this lockfile, the accepted provenance metadata in `tiktok_camofox_sync.py`, and the associated tests in the same change.