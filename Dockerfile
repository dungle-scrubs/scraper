# Pin to the declared floor (requires-python >=3.12), pinned by digest (multi-arch index).
FROM python:3.15-slim-bookworm@sha256:122756f3fe72c4f5d3d71897888204ee85774bc79de952555b8a474e1d0a5d53

# Playwright system deps for crawl4ai's headless Chromium.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

# Install uv, pinned by digest (multi-arch index; tag: 0.9.4).
COPY --from=ghcr.io/astral-sh/uv:0.9.4@sha256:c4089b0085cf4d38e38d5cdaa5e57752c1878a6f41f2e3a3a234dc5f23942cb4 /uv /usr/local/bin/uv

# Run as an unprivileged user — Chromium must not run as root.
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
RUN chown appuser:appuser /app
USER appuser

# Install dependencies first (layer caching).
COPY --chown=appuser:appuser pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

# Copy source.
COPY --chown=appuser:appuser src/ src/

# Install the package.
RUN uv sync --no-dev --frozen

# Install Playwright browsers for crawl4ai. Fail the build if this fails
# (do not mask a broken image behind `|| true`).
RUN uv run crawl4ai-setup || uv run python -m playwright install chromium

EXPOSE 8020

# Health signal for `docker run` users (compose defines its own). Uses stdlib
# urllib so we don't add a dependency just for the probe.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8020/health').read()" || exit 1

CMD ["uv", "run", "scraper", "serve"]
