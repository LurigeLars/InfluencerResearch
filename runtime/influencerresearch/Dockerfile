FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    INFLUENCER_RESEARCH_CONTAINER=1

WORKDIR /research/app

COPY requirements.txt requirements_tiktok_impersonation.lock.txt ./

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip install --no-cache-dir --only-binary=:all: --no-deps --require-hashes -r requirements_tiktok_impersonation.lock.txt \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip check \
    && python -m pip uninstall -y pip \
    && useradd --system --uid 10001 --create-home --home-dir /home/influencer influencer \
    && mkdir -p /research/control /research/state /research/output /research/logs /runtime/influencerresearch \
    && chown -R 10001:10001 /research /runtime/influencerresearch /opt/ms-playwright

COPY . /research/app
RUN chown -R 10001:10001 /research/app

USER 10001:10001
EXPOSE 8770
CMD ["python", "influencerresearch_mcp.py"]
