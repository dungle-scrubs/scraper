"""FastAPI server exposing the scraping pipeline over HTTP.

Two endpoints:
  POST /scrape  — scrape a URL and return cleaned markdown
  GET  /health  — liveness check
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, HttpUrl

from dendrite_scraper.scraper import ScrapeResult, resolve_cleanup_provider, scrape
from dendrite_scraper.settings import settings

logger = logging.getLogger(__name__)


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


@app.post("/scrape", response_model=ScrapeResponse)
async def scrape_endpoint(request: ScrapeRequest) -> ScrapeResponse:
    """Scrape a URL through the full pipeline and return cleaned markdown.

    @param request: Request containing the URL to scrape.
    @returns: Scrape result with cleaned markdown or error details.
    """
    result: ScrapeResult = await scrape(str(request.url))
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
