# Pin to a specific minor tag; for production pin by digest (FROM python@sha256:...).
FROM python:3.13-slim-bookworm

# Playwright system deps for crawl4ai's headless Chromium.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

# Install uv (pin by version; for production pin by digest).
COPY --from=ghcr.io/astral-sh/uv:0.9.4 /uv /usr/local/bin/uv

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

CMD ["uv", "run", "dendrite-scraper"]
