FROM node:22.23.2-trixie-slim

ARG CAMOUFOX_VERSION=152.0.4
ARG CAMOUFOX_RELEASE=beta.28
ARG CAMOUFOX_ARCH=x86_64
ARG CAMOUFOX_SHA256=924f3109ccd6d47cd6a0384d67a345fadf975d48b6319f8dbbd5954c588982bd

ENV NODE_ENV=production \
    CAMOFOX_SKIP_DOWNLOAD=1 \
    CAMOUFOX_EXECUTABLE=/opt/camoufox/camoufox-bin

RUN apt-get update \
    && apt-get -y upgrade \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        fonts-liberation \
        fonts-noto-color-emoji \
        fontconfig \
        libasound2 \
        libdbus-glib-1-2 \
        libegl1 \
        libgbm1 \
        libgl1-mesa-dri \
        libgtk-3-0 \
        libx11-xcb1 \
        libxcomposite1 \
        libxcursor1 \
        libxdamage1 \
        libxfixes3 \
        libxi6 \
        libxrandr2 \
        libxrender1 \
        libxss1 \
        libxt6 \
        libxtst6 \
        python3 \
        unzip \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY runtime/camofox/package.json runtime/camofox/package-lock.json ./

# Disable every lifecycle script during dependency resolution. Keep unrelated
# optional dependencies omitted; the required impit Linux binding is a direct
# runtime dependency. Build only better-sqlite3 from source.
RUN npm ci --ignore-scripts --omit=dev --omit=optional --no-audit --no-fund \
    && npm_config_build_from_source=true npm rebuild better-sqlite3 --foreground-scripts --no-audit --no-fund \
    && node -e "const fs=require('fs');const path=require('path');const expected={'@askjo/camofox-browser':'1.17.0','camoufox-js':'0.11.5','impit-linux-x64-gnu':'0.14.5'};for(const [name,version] of Object.entries(expected)){const p=path.join('/app/node_modules',...name.split('/'),'package.json');const actual=JSON.parse(fs.readFileSync(p,'utf8')).version;if(actual!==version)throw new Error(name+': expected '+version+', got '+actual);}" \
    && node -e "require('impit');" \
    && node -e "const Database=require('better-sqlite3');const db=new Database(':memory:');if(db.prepare('select 1 as ok').get().ok!==1)process.exit(2);db.close();"

# The npm package's postinstall fetch is intentionally not used because it is
# dynamic. Bake the exact reviewed Camoufox Linux bundle and verify its release digest.
RUN mkdir -p /opt/camoufox \
    && curl -fL -o /tmp/camoufox.zip "https://github.com/daijro/camoufox/releases/download/v${CAMOUFOX_VERSION}-${CAMOUFOX_RELEASE}/camoufox-${CAMOUFOX_VERSION}-${CAMOUFOX_RELEASE}-lin.${CAMOUFOX_ARCH}.zip" \
    && echo "${CAMOUFOX_SHA256}  /tmp/camoufox.zip" | sha256sum -c - \
    && (unzip -q /tmp/camoufox.zip -d /opt/camoufox || true) \
    && rm -f /tmp/camoufox.zip \
    && printf '{"version":"%s","release":"%s"}\n' "${CAMOUFOX_VERSION}" "${CAMOUFOX_RELEASE}" > /opt/camoufox/version.json \
    && test -x /opt/camoufox/camoufox-bin \
    && test -f /opt/camoufox/properties.json \
    && test -f /opt/camoufox/version.json \
    && test -d /opt/camoufox/fontconfig \
    && chmod -R a+rX /opt/camoufox

COPY runtime/camofox/camofox.config.json /app/node_modules/@askjo/camofox-browser/camofox.config.json

# Build-only tools and the npm/corepack CLIs are not needed by the running server.
# Removing them keeps vulnerable package-manager transitive dependencies (for
# example tar/pacote) out of the final runtime image.
RUN apt-get purge -y --auto-remove build-essential curl unzip \
    && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
        /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
    && rm -rf /var/lib/apt/lists/* /root/.npm

USER node
EXPOSE 9377
CMD ["node", "--max-old-space-size=192", "/app/node_modules/@askjo/camofox-browser/server.js"]
