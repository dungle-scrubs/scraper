"""Core scraping pipeline: crawl4ai → bot detection → Jina fallback → model cleanup → artifact stripping.

This module is the entire reason this service exists. It wraps crawl4ai with
multi-layer resilience and content cleaning that callers shouldn't have to
reimplement.

Pipeline:
  1. Crawl4AI (local Playwright) with retry on transient errors
  2. Bot detection — Cloudflare/CAPTCHA phrases + partial JS render heuristic
  3. Jina Reader fallback — free cloud re-fetch when bot-blocked or crawl fails
  4. Noise detection — link-density heuristic for nav/sidebar chrome
  5. Model cleanup — optional Hector pass to strip non-content noise
  6. Artifact stripping — regex patterns for common scraper artifacts
"""

import asyncio
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from importlib import import_module
from types import SimpleNamespace
from typing import Any, Literal, cast

import httpx

from dendrite_scraper.settings import settings

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────

JINA_READER_PREFIX = "https://r.jina.ai/"

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

ResolvedCleanupProvider = Literal["none", "hector"]


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

    # Short pages with bot-protection phrases → challenge page.
    if len(markdown) < 2000 and any(phrase in lower for phrase in BOT_DETECTION_PHRASES):
        return True

    # Partial JS rendering: rows with many empty cells between pipes.
    pipe_rows = [line for line in markdown.splitlines() if "|" in line and "---" not in line]
    if pipe_rows:
        empty_cell_rows = sum(1 for row in pipe_rows if "|  |" in row or "| |" in row)
        if empty_cell_rows > 5:
            return True

    return False


# ── Crawl4AI ─────────────────────────────────────────────────


async def crawl_url(url: str) -> tuple[str, bool]:
    """Crawl a single URL with Crawl4AI and return (markdown, is_error).

    Suppresses crawl4ai's stdout pollution (progress bars bypass Python logging
    and corrupt JSON protocols in subprocess mode).

    @param url: URL to scrape.
    @returns: Tuple of (content_or_error_message, is_error).
    """
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

    # Redirect stdout during crawl — crawl4ai writes progress bars directly.
    original_stdout = sys.stdout
    sys.stdout = sys.stderr

    try:
        async with AsyncWebCrawler(config=browser_config) as crawler:
            result = await asyncio.wait_for(
                crawler.arun(url=url, config=run_config),
                timeout=settings.crawl_timeout_seconds,
            )
    except TimeoutError:
        return f"Crawl timed out after {settings.crawl_timeout_seconds}s", True
    finally:
        sys.stdout = original_stdout

    if not result.success:
        return f"Crawl failed: {result.error_message}", True

    markdown = result.markdown or ""
    if not markdown.strip():
        return "No markdown content returned", True

    return markdown, False


# ── Jina Reader fallback ─────────────────────────────────────


async def jina_fetch(url: str) -> tuple[str, bool]:
    """Fetch a URL via Jina Reader (free, no auth required for <20 req/min).

    Jina Reader runs headless Chromium on Google Cloud and bypasses most
    bot-protection. Used as a fallback when crawl4ai hits Cloudflare/CAPTCHA.

    @param url: URL to fetch.
    @returns: Tuple of (content_or_error_message, is_error).
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{JINA_READER_PREFIX}{url}",
                headers={"Accept": "text/markdown"},
                timeout=settings.jina_timeout_seconds,
                follow_redirects=True,
            )
    except (httpx.HTTPError, Exception) as e:
        return f"Jina Reader failed: {e}", True

    if resp.status_code != 200:
        return f"Jina Reader error {resp.status_code}: {resp.text[:200]}", True

    markdown = resp.text
    if not markdown.strip():
        return "Jina Reader returned empty content", True

    return markdown, False


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

    if settings.cleanup_provider == "hector":
        return "hector" if settings.hector_provider and settings.hector_model else "none"

    if settings.hector_provider and settings.hector_model:
        return "hector"

    return "none"


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


def find_hector_resident_target(hector: Any) -> object | None:
    """Find an already-resident Hector target when direct ensure is policy-blocked.

    @param hector: Imported Hector Python SDK module.
    @returns: Minimal target object compatible with hector.chat, or None.
    """
    try:
        snapshot = hector.info(timeout=settings.hector_timeout_seconds)
    except Exception:
        return None

    if not isinstance(snapshot, dict):
        return None

    resident_models = snapshot.get("residentModels")
    if not isinstance(resident_models, list):
        return None

    for resident in resident_models:
        if not isinstance(resident, dict):
            continue
        if (
            resident.get("provider") == settings.hector_provider
            and resident.get("model") == settings.hector_model
            and isinstance(resident.get("baseUrl"), str)
            and isinstance(resident.get("model"), str)
        ):
            return SimpleNamespace(
                base_url=resident["baseUrl"],
                model=resident["model"],
            )

    return None


def hector_clean_markdown_sync(raw_markdown: str) -> str | None:
    """Post-process scraped markdown through a Hector-resolved local model.

    @param raw_markdown: Raw markdown from crawl4ai or Jina.
    @returns: Cleaned markdown string, or None if Hector cleanup failed/unavailable.
    """
    if not settings.hector_provider or not settings.hector_model:
        return None

    try:
        hector = cast(Any, import_module("hector"))

        try:
            target = hector.ensure(
                provider=settings.hector_provider,
                model=settings.hector_model,
                app_name=settings.hector_app_name,
                sweep_on_exit=settings.hector_sweep_on_exit,
                timeout=settings.hector_timeout_seconds,
            )
        except Exception:
            target = find_hector_resident_target(hector)
            if target is None:
                return None

        try:
            payload = hector.chat(
                target,
                cleanup_messages(raw_markdown),
                max_tokens=16_000,
                temperature=0,
                timeout=settings.llm_clean_timeout_seconds,
            )
        finally:
            release = getattr(target, "release", None)
            if callable(release):
                release(sweep=settings.hector_sweep_on_exit)
    except Exception:
        return None

    return extract_chat_content(payload)


async def hector_clean_markdown(raw_markdown: str) -> str | None:
    """Post-process scraped markdown through Hector without blocking the event loop.

    @param raw_markdown: Raw markdown from crawl4ai or Jina.
    @returns: Cleaned markdown string, or None if Hector cleanup failed/unavailable.
    """
    return await asyncio.to_thread(hector_clean_markdown_sync, raw_markdown)


async def llm_clean_markdown(raw_markdown: str) -> str | None:
    """Post-process scraped markdown through the configured model cleanup provider.

    @param raw_markdown: Raw markdown from crawl4ai or Jina.
    @returns: Cleaned markdown string, or None if cleanup failed/unavailable.
    """
    provider = resolve_cleanup_provider()
    if provider == "hector":
        return await hector_clean_markdown(raw_markdown)
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
            # Don't retry non-transient errors.
            if "not installed" in markdown or "timed out" in markdown:
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

    # ── Step 2: Jina Reader fallback ─────────────────────────
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
