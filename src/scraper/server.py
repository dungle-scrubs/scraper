"""FastAPI server exposing the scraping pipeline over HTTP.

Two endpoints:
  POST /scrape  — scrape a URL and return cleaned markdown
  GET  /health  — liveness check
"""

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, HttpUrl

from scraper import __version__
from scraper.safety import UrlRejected, validate_url_async
from scraper.scraper import ScrapeResult, scrape
from scraper.settings import settings

logger = logging.getLogger(__name__)

# ── ASGI primitives ──────────────────────────────────────────

# Minimal ASGI typing so the raw-middleware code below is type-checked. The
# canonical `asgiref.typing` aliases are heavier than this module needs.
type Message = dict[str, object]
type Scope = dict[str, object]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# Bounds concurrent in-flight scrapes (each can launch a Chromium); excess
# requests wait briefly then get 503. Module-level so it is shared across
# requests; safe to construct outside a running loop on Python 3.10+.
_scrape_semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)


# ── Body-size limiting ───────────────────────────────────────


class MaxBodySizeMiddleware:
    """Raw ASGI middleware that rejects oversized request bodies with 413.

    Starlette has no built-in body limit — without this, a caller can POST an
    arbitrarily large JSON body and Starlette buffers it before pydantic sees
    it. This reads the body incrementally, aborts with 413 once
    `settings.max_request_body_bytes` is exceeded, and otherwise re-injects the
    accumulated (in-cap) body for the downstream app. Implemented at the raw
    ASGI layer (not BaseHTTPMiddleware) so oversized bodies are rejected
    mid-stream rather than buffered whole — but note the in-cap streaming path
    does buffer up to `max_request_body_bytes` bytes into memory before
    re-injection.

    @param app: The wrapped ASGI app.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        cap = settings.max_request_body_bytes

        # Fast path: reject by Content-Length before reading any body. This covers
        # virtually all JSON requests (which always carry Content-Length).
        raw_headers = scope.get("headers")
        if isinstance(raw_headers, list):
            for header in raw_headers:
                if not isinstance(header, (tuple, list)) or len(header) != 2:
                    continue
                name = header[0]
                value = header[1]
                if name == b"content-length":
                    try:
                        if int(bytes(cast("bytes", value)).decode()) > cap:
                            await _emit_413(send)
                            return
                    except (ValueError, TypeError):
                        pass
                    break

        # Streaming path (chunked / no Content-Length): accumulate the body,
        # abort if it exceeds the cap.
        body = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message.get("type") == "http.disconnect":
                break
            if message.get("type") != "http.request":
                continue
            chunk = message.get("body", b"")
            if isinstance(chunk, (bytes, bytearray)):
                body.extend(chunk)
            if len(body) > cap:
                await _emit_413(send)
                return
            more_body = bool(message.get("more_body", False))

        # Re-inject the accumulated body and delegate to the app normally.
        full_body = bytes(body)

        async def replay_receive() -> Message:
            return {"type": "http.request", "body": full_body, "more_body": False}

        await self._app(scope, replay_receive, send)


async def _emit_413(send: Send) -> None:
    """Send a minimal ASGI 413 response.

    @param send: The ASGI send callable.
    """
    body = b'{"detail":"Request body too large"}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                # A 413 may fire mid-stream; force the connection closed so the
                # client can't reuse it in a half-read state.
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


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
    logger.info("scraper starting on %s:%d", settings.host, settings.port)
    yield


app = FastAPI(
    title="scraper",
    description="Scraping service with anti-bot detection and Jina fallback",
    version=__version__,
    lifespan=lifespan,
)

# Starlette has no built-in body limit — enforce one at the ASGI layer.
app.add_middleware(MaxBodySizeMiddleware)


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
    @param error: Error message if the scrape failed entirely.
    @param elapsed_ms: Wall-clock time for the full pipeline.
    @param attempts: Log of what was tried.
    """

    markdown: str
    source: str
    url: str
    bot_detected: bool
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

    # Bound concurrency FIRST: a flood of malicious URLs must not each burn a
    # (blocking) DNS resolve before any cap applies. Acquiring is cheap (no DNS);
    # blocked URLs release the slot immediately after validation.
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
        # Non-blocking validation (thread-offloaded) so DNS can't stall the loop.
        try:
            await validate_url_async(url)
        except UrlRejected as exc:
            raise HTTPException(status_code=400, detail="URL rejected") from exc

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
