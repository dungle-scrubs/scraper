"""FastAPI server exposing the scraping pipeline over HTTP.

Two endpoints:
  POST /scrape  — scrape a URL and return cleaned markdown
  GET  /health  — liveness check
"""

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, HttpUrl

from dendrite_scraper.safety import UrlRejected, validate_url
from dendrite_scraper.scraper import ScrapeResult, resolve_cleanup_provider, scrape
from dendrite_scraper.settings import settings

logger = logging.getLogger(__name__)

# Bounds concurrent in-flight scrapes (each can launch a Chromium); excess
# requests wait briefly then get 503. Module-level so it is shared across
# requests; safe to construct outside a running loop on Python 3.10+.
_scrape_semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)


def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    """Enforce the optional API key on protected endpoints.

    When `settings.api_key` is unset the check is a no-op (zero-config local
    use). When set, require a matching `Authorization: Bearer <key>` or
    `X-API-Key: <key>` header, compared in constant time.

    @param authorization: Authorization header, if present.
    @param x_api_key: X-API-Key header, if present.
    @throws HTTPException: 401 when a key is required but missing/incorrect.
    """
    expected = settings.api_key
    if not expected:
        return

    presented = x_api_key
    if presented is None and authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = token

    if presented is None or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Log startup configuration."""
    logger.info("dendrite-scraper starting on %s:%d", settings.host, settings.port)
    logger.info(
        "Model cleanup provider: %s",
        resolve_cleanup_provider(),
    )
    yield


app = FastAPI(
    title="dendrite-scraper",
    description="Scraping service with anti-bot detection, Jina fallback, and model cleanup",
    version="0.2.0",
    lifespan=lifespan,
)


# ── Request / Response models ────────────────────────────────


class ScrapeRequest(BaseModel):
    """POST /scrape request body.

    @param url: URL to scrape.
    """

    url: HttpUrl


class ScrapeResponse(BaseModel):
    """POST /scrape response body.

    @param markdown: Cleaned markdown content (empty string on failure).
    @param source: Which backend produced the content ("crawl4ai", "jina", or "none").
    @param url: The URL that was scraped.
    @param bot_detected: Whether bot protection was detected.
    @param llm_cleaned: Whether model-backed post-processing was applied.
    @param error: Error message if the scrape failed entirely.
    @param elapsed_ms: Wall-clock time for the full pipeline.
    @param attempts: Log of what was tried.
    """

    markdown: str
    source: str
    url: str
    bot_detected: bool
    llm_cleaned: bool
    error: str | None = None
    elapsed_ms: float
    attempts: list[str]


class HealthResponse(BaseModel):
    """GET /health response body.

    @param status: Always "ok" when the service is running.
    """

    status: str = "ok"


# ── Endpoints ────────────────────────────────────────────────


@app.post("/scrape", response_model=ScrapeResponse, dependencies=[Depends(require_api_key)])
async def scrape_endpoint(request: ScrapeRequest) -> ScrapeResponse:
    """Scrape a URL through the full pipeline and return cleaned markdown.

    Rejects blocked URLs up front (400), bounds concurrency (503 when the
    server is at capacity), and enforces a global per-request deadline.

    @param request: Request containing the URL to scrape.
    @returns: Scrape result with cleaned markdown or error details.
    @throws HTTPException: 400 when blocked by the SSRF guard, 503 when at capacity.
    """
    url = str(request.url)
    try:
        validate_url(url)
    except UrlRejected as exc:
        raise HTTPException(status_code=400, detail="URL rejected") from exc

    # Bound concurrency: wait briefly for a slot, else shed load with 503.
    try:
        await asyncio.wait_for(
            _scrape_semaphore.acquire(),
            timeout=settings.scrape_acquire_timeout_seconds,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503, detail="Server busy", headers={"Retry-After": "5"}
        ) from exc

    try:
        result: ScrapeResult = await asyncio.wait_for(
            scrape(url), timeout=settings.server_timeout_seconds
        )
    except TimeoutError:
        result = ScrapeResult(
            url=url, error=f"Global timeout exceeded ({settings.server_timeout_seconds}s)"
        )
    finally:
        _scrape_semaphore.release()

    return ScrapeResponse(
        markdown=result.markdown,
        source=result.source,
        url=result.url,
        bot_detected=result.bot_detected,
        llm_cleaned=result.llm_cleaned,
        error=result.error,
        elapsed_ms=result.elapsed_ms,
        attempts=result.attempts,
    )


@app.get("/health", response_model=HealthResponse)
async def health_endpoint() -> HealthResponse:
    """Liveness check.

    @returns: Status object with "ok".
    """
    return HealthResponse()
