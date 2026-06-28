"""Unit tests for the scraping pipeline.

These tests cover both the pure markdown helpers and the async orchestration
paths that actually make the service useful. All external calls are mocked —
no network and no real browser required.
"""

import builtins
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import scraper.scraper as scraper_module
from scraper.scraper import (
    _cap_markdown,
    _guard_route,
    _install_route_guard,
    _is_non_retryable_crawl_error,
    _suppressed_stdout,
    clean_markdown_content,
    crawl_url,
    is_scrape_artifact_line,
    jina_fetch,
    looks_like_bot_block,
    scrape,
)


class FakeCrawlResult:
    """Minimal Crawl4AI result stub used by crawl_url tests.

    @param success: Whether Crawl4AI reported success.
    @param markdown: Markdown returned by the crawl.
    @param error_message: Error message reported by Crawl4AI.
    """

    def __init__(self, *, success: bool, markdown: str = "", error_message: str = "") -> None:
        self.success = success
        self.markdown = markdown
        self.error_message = error_message


# Sentinel distinguishing "no crawler_strategy argument given" from an
# explicit None (which simulates a crawl4ai build exposing no strategy).
_UNSET: Any = object()


class FakeCrawlerStrategy:
    """Minimal crawl4ai crawler_strategy stub that records set_hook calls.

    @param hooks: Optional dict pre-seeded with hook name → callable.
    """

    def __init__(self, *, hooks: dict[str, object] | None = None) -> None:
        self.hooks: dict[str, object] = dict(hooks) if hooks else {}

    def set_hook(self, name: str, fn: object) -> None:
        """Record a hook attachment.

        @param name: Hook event name.
        @param fn: Hook callable.
        """
        self.hooks[name] = fn


class FakeCrawler:
    """Async context manager that mimics Crawl4AI's crawler object.

    @param result: Fake crawl result to return from arun.
    @param error: Optional exception to raise from arun.
    @param strategy: Optional crawler_strategy; defaults to a recording stub.
    @param crawler_strategy: Override (incl. None) for the exposed strategy; used
        to simulate a crawl4ai build that exposes no crawler_strategy.
    """

    def __init__(
        self,
        *,
        result: FakeCrawlResult | None = None,
        error: Exception | None = None,
        strategy: FakeCrawlerStrategy | None = None,
        crawler_strategy: FakeCrawlerStrategy | None = _UNSET,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, object]] = []
        self.crawler_strategy = (
            strategy or FakeCrawlerStrategy() if crawler_strategy is _UNSET else crawler_strategy
        )

    async def __aenter__(self) -> "FakeCrawler":
        """Return the fake crawler instance for `async with`.

        @returns: Self.
        """
        return self

    async def __aexit__(self, *_args: object) -> bool:
        """Do not suppress exceptions from the managed block.

        @param _args: Standard async context-manager exit arguments.
        @returns: False to propagate exceptions.
        """
        return False

    async def arun(self, *, url: str, config: object) -> FakeCrawlResult:
        """Record the call and return or raise the configured result.

        @param url: URL passed to Crawl4AI.
        @param config: Crawl config object.
        @returns: Fake crawl result.
        @throws Exception: Any configured crawler exception.
        """
        self.calls.append((url, config))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class FakeResponse:
    """HTTP response stub used by HTTP client tests.

    @param status_code: Response status code.
    @param text: Response body text.
    @param json_data: JSON payload returned by json().
    @param json_error: Optional error raised by json().
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        json_data: object | None = None,
        json_error: Exception | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._json_data = json_data
        self._json_error = json_error

    def json(self) -> object:
        """Return or fail with the configured JSON payload.

        @returns: Configured JSON payload.
        @throws Exception: Configured JSON decoding error.
        """
        if self._json_error is not None:
            raise self._json_error
        return self._json_data


class FakeStreamResponse:
    """Streaming HTTP response stub for httpx.AsyncClient.stream().

    @param status_code: Response status code.
    @param text: Body text (also the default single chunk and aread() payload).
    @param headers: Response headers.
    @param chunks: Explicit byte chunks for aiter_bytes (overrides text).
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._chunks = chunks if chunks is not None else [text.encode()]

    async def aiter_bytes(self) -> "AsyncIterator[bytes]":
        for chunk in self._chunks:
            yield chunk

    async def aread(self) -> bytes:
        return self.text.encode()


class _FakeStreamCM:
    """Async context manager returned by FakeAsyncClient.stream()."""

    def __init__(self, response: FakeStreamResponse | None, error: Exception | None) -> None:
        self._response = response
        self._error = error

    async def __aenter__(self) -> FakeStreamResponse:
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response

    async def __aexit__(self, *_args: object) -> bool:
        return False


class FakeAsyncClient:
    """Async httpx.AsyncClient substitute with programmable responses.

    @param get_response: Response returned by get().
    @param post_response: Response returned by post().
    @param get_error: Optional exception raised by get().
    @param post_error: Optional exception raised by post().
    @param stream_response: Streaming response returned by stream().
    @param stream_error: Optional exception raised on entering stream().
    """

    def __init__(
        self,
        *,
        get_response: FakeResponse | None = None,
        post_response: FakeResponse | None = None,
        get_error: Exception | None = None,
        post_error: Exception | None = None,
        stream_response: FakeStreamResponse | None = None,
        stream_error: Exception | None = None,
    ) -> None:
        self.get_response = get_response
        self.post_response = post_response
        self.get_error = get_error
        self.post_error = post_error
        self.stream_response = stream_response
        self.stream_error = stream_error
        self.last_get: tuple[tuple[object, ...], dict[str, object]] | None = None
        self.last_post: tuple[tuple[object, ...], dict[str, object]] | None = None
        self.last_stream: tuple[tuple[object, ...], dict[str, object]] | None = None

    def stream(self, *args: object, **kwargs: object) -> _FakeStreamCM:
        """Record a stream() call and return a programmable streaming context.

        @param args: Positional request args (method, url).
        @param kwargs: Keyword request args.
        @returns: Async context manager yielding the configured stream response.
        """
        self.last_stream = (args, kwargs)
        return _FakeStreamCM(self.stream_response, self.stream_error)

    async def __aenter__(self) -> "FakeAsyncClient":
        """Return the fake client for `async with`.

        @returns: Self.
        """
        return self

    async def __aexit__(self, *_args: object) -> bool:
        """Do not suppress exceptions from the managed block.

        @param _args: Standard async context-manager exit arguments.
        @returns: False to propagate exceptions.
        """
        return False

    async def get(self, *args: object, **kwargs: object) -> FakeResponse:
        """Record a GET call and return or raise the configured outcome.

        @param args: Positional request args.
        @param kwargs: Keyword request args.
        @returns: Configured fake response.
        @throws Exception: Configured client error.
        """
        self.last_get = (args, kwargs)
        if self.get_error is not None:
            raise self.get_error
        assert self.get_response is not None
        return self.get_response

    async def post(self, *args: object, **kwargs: object) -> FakeResponse:
        """Record a POST call and return or raise the configured outcome.

        @param args: Positional request args.
        @param kwargs: Keyword request args.
        @returns: Configured fake response.
        @throws Exception: Configured client error.
        """
        self.last_post = (args, kwargs)
        if self.post_error is not None:
            raise self.post_error
        assert self.post_response is not None
        return self.post_response


# ── Helper builders ──────────────────────────────────────────


def build_fake_crawl4ai(crawler: FakeCrawler) -> ModuleType:
    """Create a fake `crawl4ai` module compatible with crawl_url().

    @param crawler: Fake crawler returned from AsyncWebCrawler context manager.
    @returns: Module-like object with the required Crawl4AI symbols.
    """
    module = ModuleType("crawl4ai")

    class BrowserConfig:
        """Minimal browser-config stub."""

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class CrawlerRunConfig:
        """Minimal run-config stub."""

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class AsyncWebCrawler:
        """Context-manager wrapper that yields the provided fake crawler."""

        def __init__(self, *, config: BrowserConfig) -> None:
            self.config = config

        async def __aenter__(self) -> FakeCrawler:
            return crawler

        async def __aexit__(self, *_args: object) -> bool:
            return False

    module_any = cast(Any, module)
    module_any.AsyncWebCrawler = AsyncWebCrawler
    module_any.BrowserConfig = BrowserConfig
    module_any.CrawlerRunConfig = CrawlerRunConfig
    return module


# ── is_scrape_artifact_line ──────────────────────────────────


class TestIsScrapeArtifactLine:
    """Tests for scrape artifact line detection."""

    def test_skip_to_content_link(self) -> None:
        assert is_scrape_artifact_line("[Skip to content](/main)")

    def test_dismiss_alert(self) -> None:
        assert is_scrape_artifact_line("Dismiss alert")

    def test_mustache_template(self) -> None:
        assert is_scrape_artifact_line("{{ message }}")

    def test_github_signed_in(self) -> None:
        assert is_scrape_artifact_line("You signed in with another tab or window.")

    def test_github_signed_out(self) -> None:
        assert is_scrape_artifact_line("You signed out in another tab or window.")

    def test_github_switched_accounts(self) -> None:
        assert is_scrape_artifact_line("You switched accounts on another tab or window.")

    def test_normal_content_not_artifact(self) -> None:
        assert not is_scrape_artifact_line("This is normal documentation content.")

    def test_empty_line_not_artifact(self) -> None:
        assert not is_scrape_artifact_line("")

    def test_whitespace_only_not_artifact(self) -> None:
        assert not is_scrape_artifact_line("   ")


# ── clean_markdown_content ───────────────────────────────────


class TestCleanMarkdownContent:
    """Tests for markdown content cleaning."""

    def test_removes_artifacts(self) -> None:
        raw = "[Skip to content](/main)\n\n# Hello\n\nWorld\n"
        cleaned = clean_markdown_content(raw)
        assert "[Skip to content]" not in cleaned
        assert "# Hello" in cleaned
        assert "World" in cleaned

    def test_collapses_duplicate_lines(self) -> None:
        raw = "# Title\n# Title\n\nContent\n"
        cleaned = clean_markdown_content(raw)
        assert cleaned.count("# Title") == 1

    def test_collapses_excessive_newlines(self) -> None:
        raw = "# Title\n\n\n\n\n\nContent\n"
        cleaned = clean_markdown_content(raw)
        assert "\n\n\n" not in cleaned

    def test_empty_input(self) -> None:
        assert clean_markdown_content("") == ""

    def test_preserves_code_blocks(self) -> None:
        raw = "# Code\n\n```python\ndef hello():\n    pass\n```\n"
        cleaned = clean_markdown_content(raw)
        assert "```python" in cleaned
        assert "def hello():" in cleaned

    def test_trailing_newline(self) -> None:
        cleaned = clean_markdown_content("Hello")
        assert cleaned.endswith("\n")


# ── looks_like_bot_block ─────────────────────────────────────


class TestLooksLikeBotBlock:
    """Tests for bot protection detection."""

    def test_cloudflare_challenge(self) -> None:
        page = "Just a moment...\nPerforming security verification\nPlease wait"
        assert looks_like_bot_block(page)

    def test_captcha_page(self) -> None:
        page = "Please complete the CAPTCHA to continue"
        assert looks_like_bot_block(page)

    def test_access_denied(self) -> None:
        page = "Access denied. You don't have permission."
        assert looks_like_bot_block(page)

    def test_enable_javascript_phrase(self) -> None:
        page = "Enable JavaScript to continue"
        assert looks_like_bot_block(page)

    def test_verifies_you_are_not_a_bot_phrase(self) -> None:
        page = "This page verifies you are not a bot"
        assert looks_like_bot_block(page)

    def test_this_page_was_lost_in_training_phrase(self) -> None:
        page = "Sorry, this page was lost in training"
        assert looks_like_bot_block(page)

    def test_long_page_not_bot_block(self) -> None:
        """Long pages with bot phrases are real content, not challenge pages."""
        page = "Cloudflare is a company\n" * 200
        assert not looks_like_bot_block(page)

    def test_partial_js_render(self) -> None:
        """Tables with many empty pipe cells indicate partial JS rendering."""
        rows = "| Name | Score | Status |\n|---|---|---|\n"
        rows += "| |  | |\n" * 10
        assert looks_like_bot_block(rows)

    def test_partial_js_render_with_tab_padding(self) -> None:
        """Empty-cell detection must be robust to tabs / varied padding."""
        rows = "| Name | Score |\n|---|---|\n"
        rows += "|\t|\t\t|\n" * 10  # tab-padded empty cells, not "|  |"
        assert looks_like_bot_block(rows)

    def test_real_table_not_flagged(self) -> None:
        rows = "| Name | Score |\n|---|---|\n"
        rows += "| Alice | 95 |\n| Bob | 87 |\n"
        assert not looks_like_bot_block(rows)

    def test_normal_content(self) -> None:
        page = "# Welcome\n\nThis is a real page with actual content.\n" * 20
        assert not looks_like_bot_block(page)

    def test_short_legit_page_mentioning_cloudflare_not_flagged(self) -> None:
        """A short doc page with a heading that merely mentions a provider isn't a challenge."""
        page = (
            "# Using Cloudflare\n\n"
            "Cloudflare is a CDN that fronts your site. This short guide shows how to "
            "configure caching rules for your project.\n"
        )
        assert not looks_like_bot_block(page)

    def test_terse_headingless_challenge_still_flagged(self) -> None:
        assert looks_like_bot_block("Just a moment...\nPerforming security verification")


class TestNonRetryableCrawlError:
    """Tests for the typed non-retryable crawl-error predicate (replaces substring sniffing)."""

    @pytest.mark.parametrize(
        "message",
        [
            "crawl4ai is not installed. Run: ...",
            "Crawl timed out after 25s",
            "Blocked: blocked-address: loopback",
        ],
    )
    def test_non_retryable(self, message: str) -> None:
        assert _is_non_retryable_crawl_error(message) is True

    @pytest.mark.parametrize(
        "message",
        ["Crawl failed: connection reset", "No markdown content returned", "transient failure"],
    )
    def test_retryable(self, message: str) -> None:
        assert _is_non_retryable_crawl_error(message) is False


# ── crawl_url ────────────────────────────────────────────────


class TestCrawlUrl:
    """Tests for the Crawl4AI wrapper."""

    @pytest.mark.asyncio
    async def test_import_error(self) -> None:
        real_import = builtins.__import__

        def fake_import(
            name: str,
            globals: Mapping[str, object] | None = None,
            locals: Mapping[str, object] | None = None,
            fromlist: Sequence[str] | None = (),
            level: int = 0,
        ) -> ModuleType:
            if name == "crawl4ai":
                raise ImportError("missing")
            return real_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=fake_import):
            message, is_error = await crawl_url("https://example.com")

        assert is_error is True
        assert "crawl4ai is not installed" in message

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        crawler = FakeCrawler(result=FakeCrawlResult(success=True, markdown="# Hello\n"))
        original_stdout = sys.stdout

        with patch.dict(sys.modules, {"crawl4ai": build_fake_crawl4ai(crawler)}):
            markdown, is_error = await crawl_url("https://example.com")

        assert is_error is False
        assert markdown == "# Hello\n"
        assert crawler.calls[0][0] == "https://example.com"
        assert sys.stdout is original_stdout

    @pytest.mark.asyncio
    async def test_timeout_restores_stdout(self) -> None:
        crawler = FakeCrawler(result=FakeCrawlResult(success=True, markdown="# Hello\n"))
        original_stdout = sys.stdout

        async def fake_wait_for(awaitable: object, timeout: object) -> object:
            """Close the pending crawl coroutine before simulating a timeout.

            @param awaitable: Awaitable created by crawl_url.
            @param timeout: Timeout value passed to asyncio.wait_for.
            @returns: Never returns.
            @throws TimeoutError: Always, to simulate a deadline breach.
            """
            del timeout
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise TimeoutError

        with (
            patch.dict(sys.modules, {"crawl4ai": build_fake_crawl4ai(crawler)}),
            patch("scraper.scraper.asyncio.wait_for", side_effect=fake_wait_for),
        ):
            message, is_error = await crawl_url("https://example.com")

        assert is_error is True
        assert "timed out" in message
        assert sys.stdout is original_stdout

    @pytest.mark.asyncio
    async def test_unsuccessful_result(self) -> None:
        crawler = FakeCrawler(result=FakeCrawlResult(success=False, error_message="blocked"))

        with patch.dict(sys.modules, {"crawl4ai": build_fake_crawl4ai(crawler)}):
            message, is_error = await crawl_url("https://example.com")

        assert is_error is True
        assert message == "Crawl failed: blocked"

    @pytest.mark.asyncio
    async def test_empty_markdown(self) -> None:
        crawler = FakeCrawler(result=FakeCrawlResult(success=True, markdown="   "))

        with patch.dict(sys.modules, {"crawl4ai": build_fake_crawl4ai(crawler)}):
            message, is_error = await crawl_url("https://example.com")

        assert is_error is True
        assert message == "No markdown content returned"

    @pytest.mark.asyncio
    async def test_attaches_route_guard_via_set_hook(self) -> None:
        """crawl_url must register the guard hook on the crawler strategy (F6)."""
        crawler = FakeCrawler(result=FakeCrawlResult(success=True, markdown="# Hi\n"))

        with patch.dict(sys.modules, {"crawl4ai": build_fake_crawl4ai(crawler)}):
            await crawl_url("https://example.com")

        assert crawler.crawler_strategy is not None
        assert "on_page_context_created" in crawler.crawler_strategy.hooks

    @pytest.mark.asyncio
    async def test_missing_strategy_raises(self) -> None:
        """A missing crawler_strategy must fail loud, not silently fail open (F1)."""
        crawler = FakeCrawler(
            result=FakeCrawlResult(success=True, markdown="# Hi\n"),
            crawler_strategy=None,
        )

        with (
            patch.dict(sys.modules, {"crawl4ai": build_fake_crawl4ai(crawler)}),
            pytest.raises(RuntimeError, match="crawler_strategy not found"),
        ):
            await crawl_url("https://example.com")

    @pytest.mark.asyncio
    async def test_strategy_without_set_hook_raises(self) -> None:
        """A strategy lacking set_hook must fail loud (F1)."""

        class HooklessStrategy:
            pass

        crawler = FakeCrawler(
            result=FakeCrawlResult(success=True, markdown="# Hi\n"),
            strategy=cast("FakeCrawlerStrategy", HooklessStrategy()),  # type: ignore[arg-type]
        )

        with (
            patch.dict(sys.modules, {"crawl4ai": build_fake_crawl4ai(crawler)}),
            pytest.raises(RuntimeError, match="no set_hook"),
        ):
            await crawl_url("https://example.com")


# ── jina_fetch ───────────────────────────────────────────────


class TestJinaFetch:
    """Tests for the Jina fallback fetcher."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        client = FakeAsyncClient(
            stream_response=FakeStreamResponse(status_code=200, text="# Jina\n")
        )

        with patch("scraper.scraper.httpx.AsyncClient", return_value=client):
            markdown, is_error = await jina_fetch("https://example.com")

        assert is_error is False
        assert markdown == "# Jina\n"
        assert client.last_stream is not None
        assert client.last_stream[0] == ("GET", "https://r.jina.ai/https://example.com")

    @pytest.mark.asyncio
    async def test_disables_redirects(self) -> None:
        """Jina fetch must pass follow_redirects=False so a Jina 3xx can't be
        followed to an unvalidated host."""
        client = FakeAsyncClient(
            stream_response=FakeStreamResponse(status_code=200, text="# Jina\n")
        )

        with patch("scraper.scraper.httpx.AsyncClient", return_value=client):
            await jina_fetch("https://example.com")

        assert client.last_stream is not None
        kwargs = client.last_stream[1]
        assert kwargs.get("follow_redirects") is False

    @pytest.mark.asyncio
    async def test_http_error(self) -> None:
        client = FakeAsyncClient(stream_error=httpx.HTTPError("network down"))

        with patch("scraper.scraper.httpx.AsyncClient", return_value=client):
            message, is_error = await jina_fetch("https://example.com")

        assert is_error is True
        assert "Jina Reader failed" in message

    @pytest.mark.asyncio
    async def test_non_200(self) -> None:
        client = FakeAsyncClient(
            stream_response=FakeStreamResponse(status_code=403, text="forbidden")
        )

        with patch("scraper.scraper.httpx.AsyncClient", return_value=client):
            message, is_error = await jina_fetch("https://example.com")

        assert is_error is True
        assert message == "Jina Reader error 403"
        assert "forbidden" not in message  # upstream body not reflected (M11)

    @pytest.mark.asyncio
    async def test_empty_body(self) -> None:
        client = FakeAsyncClient(stream_response=FakeStreamResponse(status_code=200, text="   "))

        with patch("scraper.scraper.httpx.AsyncClient", return_value=client):
            message, is_error = await jina_fetch("https://example.com")

        assert is_error is True
        assert message == "Jina Reader returned empty content"


# ── scrape orchestration ─────────────────────────────────────


class TestScrape:
    """Tests for the top-level scrape pipeline."""

    @pytest.mark.asyncio
    async def test_crawl4ai_success(self) -> None:
        with (
            patch(
                "scraper.scraper.crawl_url",
                new=AsyncMock(return_value=("# Title\n", False)),
            ),
            patch("scraper.scraper.jina_fetch", new=AsyncMock()) as mock_jina,
        ):
            result = await scrape("https://example.com")

        assert result.source == "crawl4ai"
        assert result.markdown == "# Title\n"
        assert result.error is None
        assert result.attempts == ["crawl4ai attempt 1"]
        mock_jina.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_then_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "max_retries", 2)
        monkeypatch.setattr(scraper_module.settings, "retry_delay_seconds", 0)

        with (
            patch(
                "scraper.scraper.crawl_url",
                new=AsyncMock(side_effect=[("transient failure", True), ("# Final\n", False)]),
            ),
            patch("scraper.scraper.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            result = await scrape("https://example.com")

        assert result.source == "crawl4ai"
        assert result.markdown == "# Final\n"
        assert result.attempts == ["crawl4ai attempt 1", "crawl4ai attempt 2"]
        mock_sleep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bot_detection_falls_back_to_jina(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "jina_enabled", True)
        with (
            patch(
                "scraper.scraper.crawl_url",
                new=AsyncMock(return_value=("Just a moment", False)),
            ),
            patch(
                "scraper.scraper.jina_fetch",
                new=AsyncMock(return_value=("# Jina\n", False)),
            ),
        ):
            result = await scrape("https://example.com")

        assert result.source == "jina"
        assert result.markdown == "# Jina\n"
        assert result.bot_detected is True
        assert result.attempts == [
            "crawl4ai attempt 1",
            "bot protection detected",
            "jina fallback",
        ]

    @pytest.mark.asyncio
    async def test_non_transient_timeout_breaks_retry_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scraper_module.settings, "max_retries", 3)
        monkeypatch.setattr(scraper_module.settings, "jina_enabled", True)

        with (
            patch(
                "scraper.scraper.crawl_url",
                new=AsyncMock(return_value=("Crawl timed out after 25s", True)),
            ) as mock_crawl,
            patch(
                "scraper.scraper.jina_fetch",
                new=AsyncMock(return_value=("Jina Reader failed: nope", True)),
            ),
        ):
            result = await scrape("https://example.com")

        assert result.source == "none"
        assert result.error == "Crawl timed out after 25s"
        assert result.attempts == ["crawl4ai attempt 1", "jina fallback"]
        assert mock_crawl.await_count == 1

    @pytest.mark.asyncio
    async def test_crawl_exception_uses_last_crash_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scraper_module.settings, "max_retries", 2)
        monkeypatch.setattr(scraper_module.settings, "retry_delay_seconds", 0)
        monkeypatch.setattr(scraper_module.settings, "jina_enabled", True)

        with (
            patch(
                "scraper.scraper.crawl_url",
                new=AsyncMock(side_effect=[RuntimeError("boom 1"), RuntimeError("boom 2")]),
            ),
            patch(
                "scraper.scraper.jina_fetch",
                new=AsyncMock(return_value=("Jina Reader failed: nope", True)),
            ),
            patch("scraper.scraper.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            result = await scrape("https://example.com")

        assert result.source == "none"
        assert result.error == "Scrape crashed: boom 2"
        assert result.attempts == ["crawl4ai attempt 1", "crawl4ai attempt 2", "jina fallback"]
        mock_sleep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_jina_success_strips_artifacts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "jina_enabled", True)
        # Raw Jina output carrying a known scraper artifact line.
        raw = "[Skip to content](/main)\n\n# Real content\n"

        with (
            patch(
                "scraper.scraper.crawl_url",
                new=AsyncMock(return_value=("Just a moment", False)),
            ),
            patch("scraper.scraper.jina_fetch", new=AsyncMock(return_value=(raw, False))),
        ):
            result = await scrape("https://example.com")

        assert result.source == "jina"
        assert "[Skip to content]" not in result.markdown
        assert "# Real content" in result.markdown


# ── SSRF guard integration ───────────────────────────────────


class FakeRoute:
    """Minimal Playwright route stub for route-guard tests."""

    def __init__(self, url: str, *, is_nav: bool = False) -> None:
        self.request = SimpleNamespace(url=url, is_navigation_request=lambda: is_nav)
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class TestSsrfGuard:
    """Tests that the SSRF guard is enforced on every fetch path."""

    @pytest.mark.asyncio
    async def test_scrape_rejects_internal_url_without_fetching(self) -> None:
        with (
            patch("scraper.scraper.crawl_url", new=AsyncMock()) as mock_crawl,
            patch("scraper.scraper.jina_fetch", new=AsyncMock()) as mock_jina,
        ):
            result = await scrape("http://169.254.169.254/latest/meta-data/")

        assert result.source == "none"
        assert result.markdown == ""
        assert result.error is not None
        assert "rejected" in result.error
        mock_crawl.assert_not_called()
        mock_jina.assert_not_called()

    @pytest.mark.asyncio
    async def test_crawl_url_blocks_internal_before_browser(self) -> None:
        # No crawl4ai module patched in — a blocked URL must return before import/launch.
        message, is_error = await crawl_url("http://127.0.0.1:8020/admin")
        assert is_error is True
        assert message.startswith("Blocked:")

    @pytest.mark.asyncio
    async def test_jina_disabled_by_default(self) -> None:
        with (
            patch(
                "scraper.scraper.crawl_url",
                new=AsyncMock(return_value=("Just a moment", False)),
            ),
            patch("scraper.scraper.jina_fetch", new=AsyncMock()) as mock_jina,
        ):
            result = await scrape("https://protected.example.com")

        assert result.source == "none"
        assert "jina disabled" in result.attempts
        mock_jina.assert_not_called()

    @pytest.mark.asyncio
    async def test_jina_strips_credentials_before_egress(self) -> None:
        client = FakeAsyncClient(
            stream_response=FakeStreamResponse(status_code=200, text="# Jina\n")
        )

        with patch("scraper.scraper.httpx.AsyncClient", return_value=client):
            markdown, is_error = await jina_fetch("https://user:pass@example.com/x")

        assert is_error is False
        assert client.last_stream is not None
        sent_url = cast(str, client.last_stream[0][1])
        assert sent_url == "https://r.jina.ai/https://example.com/x"
        assert "user" not in sent_url and "pass" not in sent_url

    @pytest.mark.asyncio
    async def test_jina_blocks_internal_target(self) -> None:
        message, is_error = await jina_fetch("http://10.0.0.5/secret")
        assert is_error is True
        assert "blocked" in message.lower()

    @pytest.mark.asyncio
    async def test_route_guard_aborts_internal_request(self) -> None:
        route = FakeRoute("http://169.254.169.254/latest/meta-data/")
        await _guard_route(route)
        assert route.aborted is True
        assert route.continued is False

    @pytest.mark.asyncio
    async def test_route_guard_allows_public_request(self) -> None:
        route = FakeRoute("https://example.com/page")
        await _guard_route(route)
        assert route.continued is True
        assert route.aborted is False

    @pytest.mark.asyncio
    async def test_install_route_guard_attaches_handler(self) -> None:
        """The hook must find the page among its args and call page.route()."""
        attached: list[str] = []

        class FakePage:
            # page-detector predicate checks for both attrs.
            goto = object

            async def route(self, pattern: str, _handler: object) -> None:
                attached.append(pattern)

        page = FakePage()
        result = await _install_route_guard(page)

        assert result is page
        assert attached == ["**/*"]

    @pytest.mark.asyncio
    async def test_install_route_guard_fails_loud_without_page(self) -> None:
        """No page-like object in the hook args must raise, not silently fail open."""
        with pytest.raises(RuntimeError, match="delivered no page"):
            await _install_route_guard("not-a-page", 42)


# ── Resource bounding (M9, M10) ──────────────────────────────


class TestResourceBounding:
    """Tests for size caps and concurrency-safe stdout suppression."""

    def test_cap_markdown_truncates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "max_markdown_chars", 10)
        assert _cap_markdown("x" * 100) == "x" * 10

    def test_cap_markdown_keeps_small(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "max_markdown_chars", 10)
        assert _cap_markdown("short") == "short"

    @pytest.mark.asyncio
    async def test_crawl_caps_markdown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "max_markdown_chars", 10)
        crawler = FakeCrawler(result=FakeCrawlResult(success=True, markdown="y" * 100))

        with patch.dict(sys.modules, {"crawl4ai": build_fake_crawl4ai(crawler)}):
            markdown, is_error = await crawl_url("https://example.com")

        assert is_error is False
        assert markdown == "y" * 10

    @pytest.mark.asyncio
    async def test_jina_rejects_oversized_by_content_length(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scraper_module.settings, "jina_max_bytes", 1000)
        client = FakeAsyncClient(
            stream_response=FakeStreamResponse(
                status_code=200, text="ok", headers={"content-length": "9999999"}
            )
        )
        with patch("scraper.scraper.httpx.AsyncClient", return_value=client):
            message, is_error = await jina_fetch("https://example.com")

        assert is_error is True
        assert "too large" in message

    @pytest.mark.asyncio
    async def test_jina_aborts_oversized_stream_without_content_length(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No Content-Length header: the byte cap must trip mid-stream (DF-2).
        monkeypatch.setattr(scraper_module.settings, "jina_max_bytes", 10)
        client = FakeAsyncClient(
            stream_response=FakeStreamResponse(
                status_code=200, chunks=[b"x" * 8, b"x" * 8, b"x" * 8]
            )
        )
        with patch("scraper.scraper.httpx.AsyncClient", return_value=client):
            message, is_error = await jina_fetch("https://example.com")

        assert is_error is True
        assert "too large" in message

    @pytest.mark.asyncio
    async def test_route_guard_aborts_after_max_redirects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scraper.scraper import _make_route_guard

        monkeypatch.setattr(scraper_module.settings, "max_redirects", 2)
        guard = _make_route_guard()
        # Allowed: 1 initial navigation + 2 redirect hops = 3; the 4th aborts.
        nav_routes = [FakeRoute("https://example.com/", is_nav=True) for _ in range(4)]
        for route in nav_routes[:3]:
            await guard(route)
        await guard(nav_routes[3])

        assert all(r.continued for r in nav_routes[:3])
        assert nav_routes[3].aborted is True
        assert nav_routes[3].continued is False

    def test_suppressed_stdout_nested_restores(self) -> None:
        """Nested (concurrent-style) suppression must only restore at the outermost exit."""
        original = sys.stdout
        with _suppressed_stdout():
            assert sys.stdout is not original
            with _suppressed_stdout():
                assert sys.stdout is not original
            # Inner exit must NOT restore while an outer scope is still active.
            assert sys.stdout is not original
        assert sys.stdout is original
