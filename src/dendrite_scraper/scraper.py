"""Core scraping pipeline: crawl4ai → bot detection → Jina fallback → model cleanup → artifact stripping.

This module is the entire reason this service exists. It wraps crawl4ai with
multi-layer resilience and content cleaning that callers shouldn't have to
reimplement.

Pipeline:
  1. Crawl4AI (local Playwright) with retry on transient errors
  2. Bot detection — Cloudflare/CAPTCHA phrases + partial JS render heuristic
  3. Jina Reader fallback — free cloud re-fetch when bot-blocked or crawl fails
  4. Noise detection — link-density heuristic for nav/sidebar chrome
  5. Model cleanup — optional EmberLM local-model pass to strip non-content noise
  6. Artifact stripping — regex patterns for common scraper artifacts
"""

import asyncio
import contextlib
import logging
import re
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import httpx

from dendrite_scraper.safety import UrlRejected, validate_url
from dendrite_scraper.settings import settings

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────

JINA_READER_PREFIX = "https://r.jina.ai/"

# Crawl errors that should not be retried (vs. transient failures). Matched by
# prefix against crawl_url's own messages — not arbitrary page content.
NON_RETRYABLE_CRAWL_PREFIXES = (
    "crawl4ai is not installed",
    "Crawl timed out",
    "Blocked:",
)

BOT_DETECTION_PHRASES = (
    "performing security verification",
    "verifies you are not a bot",
    "cloudflare",
    "captcha",
    "enable javascript to continue",
    "this page was lost in training",
    "access denied",
    "just a moment",
)

SCRAPE_ARTIFACT_LINE_PATTERNS = (
    re.compile(r"^\[Skip to content\]\([^)]*\)$", re.IGNORECASE),
    re.compile(r"^Dismiss alert$", re.IGNORECASE),
    re.compile(r"^\{\{\s*message\s*\}\}$", re.IGNORECASE),
)

SCRAPE_ARTIFACT_LINE_SUBSTRINGS = (
    "You signed in with another tab or window.",
    "You signed out in another tab or window.",
    "You switched accounts on another tab or window.",
)

CLEANUP_SYSTEM_PROMPT = (
    "You are a documentation extractor. Extract ONLY the main documentation content from the "
    "scraped markdown below. Remove navigation menus, sidebars, footers, cookie notices, ads, "
    "breadcrumbs, table of contents, sign-in prompts, and any non-documentation chrome. Preserve "
    "headings, paragraphs, code blocks, lists, tables, links within the content, and all technical "
    "detail. Return clean markdown only. No commentary."
)

ResolvedCleanupProvider = Literal["none", "emberlm"]


# ── Result types ─────────────────────────────────────────────


@dataclass
class ScrapeResult:
    """Outcome of the scraping pipeline.

    @param markdown: Cleaned markdown content (empty string on failure).
    @param source: Which backend produced the content.
    @param url: The URL that was scraped.
    @param bot_detected: Whether bot protection was detected on the initial crawl.
    @param llm_cleaned: Whether model-backed markdown cleanup was applied.
    @param error: Error message if the scrape failed entirely.
    @param elapsed_ms: Wall-clock time for the full pipeline.
    @param attempts: Log of what was tried.
    """

    markdown: str = ""
    source: str = "none"
    url: str = ""
    bot_detected: bool = False
    llm_cleaned: bool = False
    error: str | None = None
    elapsed_ms: float = 0.0
    attempts: list[str] = field(default_factory=list)


# ── Resource bounding ────────────────────────────────────────

# Depth-counted so concurrent crawls in one event loop don't corrupt sys.stdout:
# only the outermost entry saves/restores the real stream (a naive save/restore
# per call would capture another in-flight call's already-swapped stream).
_stdout_suppress_depth = 0
_stdout_real: Any = None


@contextlib.contextmanager
def _suppressed_stdout() -> Iterator[None]:
    """Silence direct stdout writes (crawl4ai progress bars) — concurrency-safe.

    @returns: Context manager that routes stdout to stderr for its duration.
    """
    global _stdout_suppress_depth, _stdout_real
    if _stdout_suppress_depth == 0:
        _stdout_real = sys.stdout
        sys.stdout = sys.stderr
    _stdout_suppress_depth += 1
    try:
        yield
    finally:
        _stdout_suppress_depth -= 1
        if _stdout_suppress_depth == 0:
            sys.stdout = _stdout_real
            _stdout_real = None


def _is_non_retryable_crawl_error(message: str) -> bool:
    """Return True for crawl errors that retrying cannot fix.

    @param message: Error message returned by `crawl_url`.
    @returns: True when the crawl should not be retried.
    """
    return message.startswith(NON_RETRYABLE_CRAWL_PREFIXES)


def _cap_markdown(markdown: str) -> str:
    """Bound markdown length before it reaches the heuristics and cleaners.

    @param markdown: Raw markdown from any source.
    @returns: Markdown truncated to `settings.max_markdown_chars`.
    """
    cap = settings.max_markdown_chars
    return markdown if len(markdown) <= cap else markdown[:cap]


# ── Artifact stripping ───────────────────────────────────────


def is_scrape_artifact_line(line: str) -> bool:
    """Return True when a markdown line matches known scraper noise patterns.

    @param line: Single line of markdown text.
    @returns: True if this line is a scraper artifact that should be removed.
    """
    stripped = line.strip()
    if not stripped:
        return False

    if any(pattern.match(stripped) for pattern in SCRAPE_ARTIFACT_LINE_PATTERNS):
        return True

    return any(fragment in stripped for fragment in SCRAPE_ARTIFACT_LINE_SUBSTRINGS)


def clean_markdown_content(markdown: str) -> str:
    """Remove known scrape artifacts and normalize markdown whitespace.

    @param markdown: Raw markdown from any scraping source.
    @returns: Cleaned markdown with artifacts removed and whitespace normalized.
    """
    cleaned_lines: list[str] = []

    for line in markdown.splitlines():
        normalized = line.rstrip()
        if is_scrape_artifact_line(normalized):
            continue

        # Collapse duplicate adjacent lines from page chrome extraction.
        if normalized and cleaned_lines and normalized == cleaned_lines[-1]:
            continue

        cleaned_lines.append(normalized)

    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return f"{cleaned}\n" if cleaned else ""


# ── Bot detection ────────────────────────────────────────────


def looks_like_bot_block(markdown: str) -> bool:
    """Return True when scraped markdown looks like a bot-protection or failed-render page.

    Detects two failure modes:
    1. Cloudflare/CAPTCHA challenge pages (short, contains bot-protection phrases).
    2. Partial JS rendering — page structure loaded but dynamic data is missing,
       e.g. table rows with blank cells where content should be.

    @param markdown: Markdown from crawl4ai.
    @returns: True if the content should be re-fetched via Jina Reader.
    """
    lower = markdown.lower()

    # Challenge pages are short, heading-less, and terse. Requiring all three
    # avoids false positives on legitimate short docs that merely mention a
    # provider name like "cloudflare".
    has_phrase = any(phrase in lower for phrase in BOT_DETECTION_PHRASES)
    has_heading = any(line.lstrip().startswith("#") for line in markdown.splitlines())
    word_count = len(lower.split())
    if has_phrase and not has_heading and len(markdown) < 1500 and word_count < 150:
        return True

    # Partial JS rendering: rows with many empty cells between pipes.
    pipe_rows = [line for line in markdown.splitlines() if "|" in line and "---" not in line]
    if pipe_rows:
        empty_cell_rows = sum(1 for row in pipe_rows if "|  |" in row or "| |" in row)
        if empty_cell_rows > 5:
            return True

    return False


# ── Crawl4AI ─────────────────────────────────────────────────


async def _guard_route(route: Any) -> None:
    """Playwright route handler: abort any in-browser request to a blocked host.

    Fires on every request the page makes, including redirect hops, so a public
    URL that 3xx-redirects toward an internal/metadata host is aborted rather
    than followed.

    @param route: Playwright route for the intercepted request.
    """
    try:
        validate_url(route.request.url)
    except UrlRejected:
        await route.abort()
        return
    await route.continue_()


def _make_route_guard() -> Any:
    """Build a per-page route handler that host-validates and caps redirect hops.

    Each page context gets a fresh handler with its own navigation counter so a
    redirect loop to public hosts can't spin forever (DoS), on top of the per-hop
    host validation that closes SSRF.

    @returns: An async Playwright route handler.
    """
    nav_count = 0

    async def guard(route: Any) -> None:
        nonlocal nav_count
        is_nav = getattr(route.request, "is_navigation_request", None)
        if callable(is_nav) and is_nav():
            nav_count += 1
            # Allow the initial navigation plus `max_redirects` hops.
            if nav_count > settings.max_redirects + 1:
                await route.abort()
                return
        await _guard_route(route)

    return guard


async def _install_route_guard(*args: Any, **kwargs: Any) -> Any:
    """crawl4ai `on_page_context_created` hook: attach the route guard to the page.

    Locates the Playwright page among the hook arguments (signatures vary across
    crawl4ai versions) and installs a fresh counting route guard on all requests.

    @param args: Positional hook arguments.
    @param kwargs: Keyword hook arguments.
    @returns: The page object, when found (crawl4ai expects the page back).
    """
    candidates = [*args, *kwargs.values()]
    page = next((c for c in candidates if hasattr(c, "route") and hasattr(c, "goto")), None)
    if page is not None:
        await page.route("**/*", _make_route_guard())
    return page


async def crawl_url(url: str) -> tuple[str, bool]:
    """Crawl a single URL with Crawl4AI and return (markdown, is_error).

    Suppresses crawl4ai's stdout pollution (progress bars bypass Python logging
    and corrupt JSON protocols in subprocess mode).

    @param url: URL to scrape.
    @returns: Tuple of (content_or_error_message, is_error).
    """
    # Pre-flight SSRF guard: reject before launching a browser at all.
    try:
        validate_url(url)
    except UrlRejected as exc:
        return f"Blocked: {exc.reason}", True

    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    except ImportError:
        return "crawl4ai is not installed. Run: pip install crawl4ai && crawl4ai-setup", True

    # Suppress crawl4ai's own logging.
    for logger_name in ("crawl4ai", "crawl4ai.async_webcrawler", "crawl4ai.async_crawler_strategy"):
        logging.getLogger(logger_name).setLevel(logging.CRITICAL)

    browser_config = BrowserConfig(headless=True, verbose=False)
    run_config = CrawlerRunConfig(
        word_count_threshold=10,
        exclude_external_links=True,
        excluded_tags=["nav", "footer", "header", "aside"],
        process_iframes=False,
        page_timeout=15000,
        delay_before_return_html=2.0,
    )

    # Suppress crawl4ai's direct stdout writes (progress bars) for the crawl's
    # duration without corrupting stdout across concurrent requests.
    try:
        with _suppressed_stdout():
            async with AsyncWebCrawler(config=browser_config) as crawler:
                # Re-validate every in-browser request/redirect hop (defeats
                # redirect bypass; covers what the initial pre-flight cannot).
                strategy = getattr(crawler, "crawler_strategy", None)
                if strategy is not None and hasattr(strategy, "set_hook"):
                    strategy.set_hook("on_page_context_created", _install_route_guard)

                result = await asyncio.wait_for(
                    crawler.arun(url=url, config=run_config),
                    timeout=settings.crawl_timeout_seconds,
                )
    except TimeoutError:
        return f"Crawl timed out after {settings.crawl_timeout_seconds}s", True

    if not result.success:
        return f"Crawl failed: {result.error_message}", True

    markdown = result.markdown or ""
    if not markdown.strip():
        return "No markdown content returned", True

    return _cap_markdown(markdown), False


# ── Jina Reader fallback ─────────────────────────────────────


async def jina_fetch(url: str) -> tuple[str, bool]:
    """Fetch a URL via Jina Reader (free, no auth required for <20 req/min).

    Jina Reader runs headless Chromium on Google Cloud and bypasses most
    bot-protection. Used as a fallback when crawl4ai hits Cloudflare/CAPTCHA.

    @param url: URL to fetch.
    @returns: Tuple of (content_or_error_message, is_error).
    """
    # Validate the target and strip any userinfo before it leaves to the third party.
    try:
        target = validate_url(url)
    except UrlRejected as exc:
        return f"Jina target blocked: {exc.reason}", True

    cap = settings.jina_max_bytes
    try:
        async with (
            httpx.AsyncClient() as client,
            client.stream(
                "GET",
                f"{JINA_READER_PREFIX}{target.url}",
                headers={"Accept": "text/markdown"},
                timeout=settings.jina_timeout_seconds,
                follow_redirects=False,
            ) as resp,
        ):
            # Fast reject by declared length before reading any body.
            declared = resp.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > cap:
                return f"Jina Reader response too large ({declared} bytes)", True

            if resp.status_code != 200:
                # Log upstream detail; do not reflect the body to the caller.
                body = await resp.aread()
                logger.warning(
                    "Jina Reader %d for %s: %s", resp.status_code, target.host, body[:200]
                )
                return f"Jina Reader error {resp.status_code}", True

            # Stream the body, aborting once it exceeds the byte cap (handles
            # chunked responses with no declared Content-Length).
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > cap:
                    return f"Jina Reader response too large (>{cap} bytes)", True
                chunks.append(chunk)
    except httpx.HTTPError as e:
        return f"Jina Reader failed: {e}", True

    markdown = b"".join(chunks).decode("utf-8", errors="replace")
    if not markdown.strip():
        return "Jina Reader returned empty content", True

    return _cap_markdown(markdown), False


# ── Model cleanup ────────────────────────────────────────────


def looks_noisy(markdown: str) -> bool:
    """Heuristic: return True when scraped markdown likely contains nav/sidebar noise.

    Checks for dense clusters of markdown links at the top, which indicate
    HTML-level excluded_tags didn't fully strip navigation chrome.

    @param markdown: Raw markdown from crawl4ai or Jina.
    @returns: True if the content looks noisy enough to benefit from model cleanup.
    """
    head = markdown[:2000]
    link_count = head.count("](")
    line_count = max(head.count("\n"), 1)

    # More than 1 link per 2 lines → likely nav/sidebar.
    return link_count > line_count / 2


def resolve_cleanup_provider() -> ResolvedCleanupProvider:
    """Resolve the configured cleanup provider into an executable backend.

    @returns: Concrete provider name or "none" when cleanup is unavailable.
    """
    if settings.cleanup_provider == "none":
        return "none"

    configured = bool(settings.emberlm_provider and settings.emberlm_model)
    if settings.cleanup_provider == "emberlm":
        return "emberlm" if configured else "none"

    # auto
    return "emberlm" if configured else "none"


def cleanup_messages(raw_markdown: str) -> list[dict[str, str]]:
    """Build chat messages for the cleanup extraction task.

    @param raw_markdown: Raw markdown from crawl4ai or Jina.
    @returns: Chat-completions-compatible messages.
    """
    truncated = raw_markdown[: settings.llm_clean_max_input_chars]
    return [
        {"role": "system", "content": CLEANUP_SYSTEM_PROMPT},
        {"role": "user", "content": truncated},
    ]


def extract_chat_content(payload: object) -> str | None:
    """Extract assistant text from a chat-completions-compatible response.

    @param payload: Response JSON payload.
    @returns: Assistant content or None when the shape is invalid/empty.
    """
    if not isinstance(payload, dict):
        return None

    response = cast(dict[str, object], payload)
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return None

    choice = cast(dict[str, object], first_choice)
    message = choice.get("message")
    if not isinstance(message, dict):
        return None

    chat_message = cast(dict[str, object], message)
    content = chat_message.get("content")
    return content if isinstance(content, str) and content.strip() else None


async def emberlm_warm() -> tuple[str, str] | None:
    """Warm the configured EmberLM model and return (base_url, qualified_id).

    EmberLM is warm-first: POST /v1/warm loads the model and blocks until ready,
    returning the OpenAI-compatible base URL to send completions to.

    @returns: (base_url, qualified_id) on success, else None.
    """
    if not settings.emberlm_provider or not settings.emberlm_model:
        return None

    body: dict[str, object] = {
        "provider": settings.emberlm_provider,
        "model": settings.emberlm_model,
        "appName": settings.emberlm_app_name,
    }
    if settings.emberlm_keep_warm_ms is not None:
        body["keepWarmMs"] = settings.emberlm_keep_warm_ms

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.emberlm_daemon_url}/v1/warm",
                json=body,
                timeout=settings.emberlm_warm_timeout_seconds,
            )
        if resp.status_code != 200:
            logger.warning("EmberLM warm failed (%d)", resp.status_code)
            return None
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("EmberLM warm unavailable: %s", exc)
        return None

    if not isinstance(payload, dict):
        return None
    base_url = payload.get("baseUrl")
    qualified_id = payload.get("qualifiedId")
    if not isinstance(base_url, str) or not isinstance(qualified_id, str):
        return None
    return base_url, qualified_id


async def emberlm_clean_markdown(raw_markdown: str) -> str | None:
    """Post-process scraped markdown through an EmberLM-warmed local model.

    Warms the model, then calls its OpenAI-compatible chat endpoint directly —
    no EmberLM SDK dependency. Calls target the local daemon, so they bypass the
    SSRF guard by design.

    @param raw_markdown: Raw markdown from crawl4ai or Jina.
    @returns: Cleaned markdown string, or None if cleanup failed/unavailable.
    """
    warmed = await emberlm_warm()
    if warmed is None:
        return None
    base_url, qualified_id = warmed

    body = {
        "model": qualified_id,
        "messages": cleanup_messages(raw_markdown),
        "max_tokens": 16_000,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json=body,
                headers={"X-EmberLM-App": settings.emberlm_app_name},
                timeout=settings.llm_clean_timeout_seconds,
            )
        if resp.status_code != 200:
            logger.warning("EmberLM chat failed (%d)", resp.status_code)
            return None
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("EmberLM chat unavailable: %s", exc)
        return None

    return extract_chat_content(payload)


async def llm_clean_markdown(raw_markdown: str) -> str | None:
    """Post-process scraped markdown through the configured model cleanup provider.

    @param raw_markdown: Raw markdown from crawl4ai or Jina.
    @returns: Cleaned markdown string, or None if cleanup failed/unavailable.
    """
    provider = resolve_cleanup_provider()
    if provider == "emberlm":
        return await emberlm_clean_markdown(raw_markdown)
    return None


# ── Pipeline orchestration ───────────────────────────────────


async def maybe_llm_clean(markdown: str) -> tuple[str, bool]:
    """Apply model-backed cleanup only when the markdown looks noisy.

    @param markdown: Raw markdown from crawl4ai or Jina.
    @returns: Tuple of (content, was_llm_cleaned).
    """
    if looks_noisy(markdown) and resolve_cleanup_provider() != "none":
        cleaned = await llm_clean_markdown(markdown)
        if cleaned:
            return cleaned, True
    return markdown, False


async def scrape(url: str) -> ScrapeResult:
    """Full scraping pipeline: crawl4ai → bot detect → Jina fallback → LLM clean → artifacts.

    This is the single entry point callers should use.

    @param url: URL to scrape.
    @returns: ScrapeResult with cleaned markdown or error details.
    """
    start = time.monotonic()
    result = ScrapeResult(url=url)

    # ── Step 0: SSRF guard — reject internal/special targets up front ──
    try:
        validate_url(url)
    except UrlRejected as exc:
        result.error = f"URL rejected: {exc.reason}"
        result.attempts.append(f"blocked: {exc.reason}")
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    # ── Step 1: Crawl4AI with retries ────────────────────────
    crawl_error: str | None = None
    for attempt in range(settings.max_retries):
        result.attempts.append(f"crawl4ai attempt {attempt + 1}")
        try:
            markdown, err = await crawl_url(url)
        except Exception as e:
            crawl_error = f"Scrape crashed: {e}"
            if attempt < settings.max_retries - 1:
                await asyncio.sleep(settings.retry_delay_seconds)
            continue

        if err:
            crawl_error = markdown
            # Don't retry non-transient errors (missing dep, timeout, blocked URL).
            if _is_non_retryable_crawl_error(markdown):
                break
            if attempt < settings.max_retries - 1:
                await asyncio.sleep(settings.retry_delay_seconds)
            continue

        # Success — check for bot block.
        if looks_like_bot_block(markdown):
            crawl_error = "Bot protection detected"
            result.bot_detected = True
            result.attempts.append("bot protection detected")
            break

        # Clean crawl4ai result.
        cleaned, was_llm_cleaned = await maybe_llm_clean(markdown)
        cleaned = clean_markdown_content(cleaned)

        result.markdown = cleaned
        result.source = "crawl4ai"
        result.llm_cleaned = was_llm_cleaned
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    # ── Step 2: Jina Reader fallback (opt-in third-party egress) ──
    if not settings.jina_enabled:
        result.attempts.append("jina disabled")
        result.error = crawl_error
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    result.attempts.append("jina fallback")
    jina_md, jina_err = await jina_fetch(url)
    if not jina_err:
        cleaned, was_llm_cleaned = await maybe_llm_clean(jina_md)
        cleaned = clean_markdown_content(cleaned)

        result.markdown = cleaned
        result.source = "jina"
        result.llm_cleaned = was_llm_cleaned
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    # ── Both failed ──────────────────────────────────────────
    result.error = crawl_error or jina_md
    result.elapsed_ms = (time.monotonic() - start) * 1000
    return result
