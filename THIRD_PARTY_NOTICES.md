# Third-party notices

This repository integrates external projects; it is not a fork of them and does not claim ownership of their code.

The dependency manifests and pinned installers are authoritative for the versions used by this project. Important components include:

| Component | Upstream | License | Relationship |
| --- | --- | --- | --- |
| yt-dlp | https://github.com/yt-dlp/yt-dlp | Unlicense | Direct Python dependency |
| Camofox Browser (`@askjo/camofox-browser`) | https://github.com/jo-inc/camofox-browser | MIT | Direct Node dependency |
| camoufox-js | https://github.com/daijro/camoufox-js | MPL-2.0 | Transitive dependency of Camofox Browser |
| Camoufox browser | https://github.com/daijro/camoufox | MPL-2.0 | Browser runtime downloaded by the reviewed Camofox postinstall |
| Playwright for Python | https://github.com/microsoft/playwright-python | Apache-2.0 | Direct Python dependency |
| faster-whisper | https://github.com/SYSTRAN/faster-whisper | MIT | Direct Python dependency |
| imageio-ffmpeg | https://github.com/imageio/imageio-ffmpeg | BSD-2-Clause | Direct Python dependency |

Additional transitive and separately installed dependencies retain their own licenses. Consult the checked-in manifests/lockfiles, installed package metadata and upstream projects before redistributing bundled third-party artifacts.