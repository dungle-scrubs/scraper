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

import dendrite_scraper.scraper as scraper_module
from dendrite_scraper.scraper import (
    _cap_markdown,
    _guard_route,
    _is_non_retryable_crawl_error,
    _suppressed_stdout,
    clean_markdown_content,
    crawl_url,
    emberlm_clean_markdown,
    is_scrape_artifact_line,
    jina_fetch,
    llm_clean_markdown,
    looks_like_bot_block,
    looks_noisy,
    maybe_llm_clean,
    resolve_cleanup_provider,
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


class FakeCrawler:
    """Async context manager that mimics Crawl4AI's crawler object.

    @param result: Fake crawl result to return from arun.
    @param error: Optional exception to raise from arun.
    """

    def __init__(
        self, *, result: FakeCrawlResult | None = None, error: Exception | None = None
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, object]] = []

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


class FakeEmberlmClient:
    """httpx.AsyncClient stub for EmberLM: routes /v1/warm vs /chat/completions.

    @param warm_response: Response for the warm POST.
    @param chat_response: Response for the chat-completions POST.
    @param warm_error: Optional exception raised on the warm POST.
    """

    def __init__(
        self,
        *,
        warm_response: FakeResponse | None = None,
        chat_response: FakeResponse | None = None,
        warm_error: Exception | None = None,
    ) -> None:
        self.warm_response = warm_response
        self.chat_response = chat_response
        self.warm_error = warm_error
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self) -> "FakeEmberlmClient":
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        """Route by URL: /v1/warm → warm response, else chat response.

        @param url: Request URL.
        @param kwargs: Request kwargs (json, headers, timeout).
        @returns: The configured fake response.
        """
        self.calls.append((url, kwargs))
        if "/v1/warm" in url:
            if self.warm_error is not None:
                raise self.warm_error
            assert self.warm_response is not None
            return self.warm_response
        assert self.chat_response is not None
        return self.chat_response


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


# ── looks_noisy ──────────────────────────────────────────────


class TestLooksNoisy:
    """Tests for noise detection heuristic."""

    def test_link_heavy_header(self) -> None:
        """Dense links in the first 2000 chars indicates nav chrome."""
        lines = [f"[Link {i}](https://example.com/{i})" for i in range(50)]
        markdown = "\n".join(lines) + "\n\n# Real Content\n\nParagraph here.\n"
        assert looks_noisy(markdown)

    def test_clean_documentation(self) -> None:
        """Normal docs with few links should not be flagged."""
        lines = [f"Paragraph {i} with some text about things." for i in range(50)]
        lines.insert(5, "[one link](https://example.com)")
        markdown = "\n".join(lines)
        assert not looks_noisy(markdown)

    def test_empty_content(self) -> None:
        assert not looks_noisy("")


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
            patch("dendrite_scraper.scraper.asyncio.wait_for", side_effect=fake_wait_for),
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


# ── jina_fetch ───────────────────────────────────────────────


class TestJinaFetch:
    """Tests for the Jina fallback fetcher."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        client = FakeAsyncClient(
            stream_response=FakeStreamResponse(status_code=200, text="# Jina\n")
        )

        with patch("dendrite_scraper.scraper.httpx.AsyncClient", return_value=client):
            markdown, is_error = await jina_fetch("https://example.com")

        assert is_error is False
        assert markdown == "# Jina\n"
        assert client.last_stream is not None
        assert client.last_stream[0] == ("GET", "https://r.jina.ai/https://example.com")

    @pytest.mark.asyncio
    async def test_http_error(self) -> None:
        client = FakeAsyncClient(stream_error=httpx.HTTPError("network down"))

        with patch("dendrite_scraper.scraper.httpx.AsyncClient", return_value=client):
            message, is_error = await jina_fetch("https://example.com")

        assert is_error is True
        assert "Jina Reader failed" in message

    @pytest.mark.asyncio
    async def test_non_200(self) -> None:
        client = FakeAsyncClient(
            stream_response=FakeStreamResponse(status_code=403, text="forbidden")
        )

        with patch("dendrite_scraper.scraper.httpx.AsyncClient", return_value=client):
            message, is_error = await jina_fetch("https://example.com")

        assert is_error is True
        assert message == "Jina Reader error 403"
        assert "forbidden" not in message  # upstream body not reflected (M11)

    @pytest.mark.asyncio
    async def test_empty_body(self) -> None:
        client = FakeAsyncClient(stream_response=FakeStreamResponse(status_code=200, text="   "))

        with patch("dendrite_scraper.scraper.httpx.AsyncClient", return_value=client):
            message, is_error = await jina_fetch("https://example.com")

        assert is_error is True
        assert message == "Jina Reader returned empty content"


# ── emberlm_clean_markdown ───────────────────────────────────


def _warm_ok() -> FakeResponse:
    return FakeResponse(
        status_code=200,
        json_data={"baseUrl": "http://127.0.0.1:17412/v1", "qualifiedId": "mlx/local-model"},
    )


class TestEmberlmCleanMarkdown:
    """Tests for the EmberLM cleanup call (warm → OpenAI-compatible chat)."""

    @pytest.mark.asyncio
    async def test_success_warms_then_chats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "emberlm_provider", "mlx")
        monkeypatch.setattr(scraper_module.settings, "emberlm_model", "local-model")
        monkeypatch.setattr(scraper_module.settings, "llm_clean_max_input_chars", 5)
        client = FakeEmberlmClient(
            warm_response=_warm_ok(),
            chat_response=FakeResponse(
                status_code=200,
                json_data={"choices": [{"message": {"content": "# Local clean\n"}}]},
            ),
        )

        with patch("dendrite_scraper.scraper.httpx.AsyncClient", return_value=client):
            cleaned = await emberlm_clean_markdown("123456789")

        assert cleaned == "# Local clean\n"
        warm_call = next(c for c in client.calls if "/v1/warm" in c[0])
        assert warm_call[0] == "http://127.0.0.1:17412/v1/warm"
        assert warm_call[1]["json"] == {
            "provider": "mlx",
            "model": "local-model",
            "appName": "dendrite-scraper",
        }
        chat_call = next(c for c in client.calls if "/chat/completions" in c[0])
        assert chat_call[0] == "http://127.0.0.1:17412/v1/chat/completions"
        chat_body = cast(dict[str, Any], chat_call[1]["json"])
        assert chat_body["model"] == "mlx/local-model"
        assert chat_body["messages"][1]["content"] == "12345"  # truncated to 5 chars
        assert cast(dict[str, str], chat_call[1]["headers"]) == {
            "X-EmberLM-App": "dendrite-scraper"
        }

    @pytest.mark.asyncio
    async def test_unconfigured_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "emberlm_provider", None)
        monkeypatch.setattr(scraper_module.settings, "emberlm_model", None)

        assert await emberlm_clean_markdown("# Hello\n") is None

    @pytest.mark.asyncio
    async def test_warm_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "emberlm_provider", "mlx")
        monkeypatch.setattr(scraper_module.settings, "emberlm_model", "local-model")
        client = FakeEmberlmClient(warm_response=FakeResponse(status_code=503, json_data={}))

        with patch("dendrite_scraper.scraper.httpx.AsyncClient", return_value=client):
            assert await emberlm_clean_markdown("# Hi\n") is None

    @pytest.mark.asyncio
    async def test_warm_unreachable_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "emberlm_provider", "mlx")
        monkeypatch.setattr(scraper_module.settings, "emberlm_model", "local-model")
        client = FakeEmberlmClient(warm_error=httpx.HTTPError("connection refused"))

        with patch("dendrite_scraper.scraper.httpx.AsyncClient", return_value=client):
            assert await emberlm_clean_markdown("# Hi\n") is None

    @pytest.mark.asyncio
    async def test_chat_failure_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "emberlm_provider", "mlx")
        monkeypatch.setattr(scraper_module.settings, "emberlm_model", "local-model")
        client = FakeEmberlmClient(
            warm_response=_warm_ok(),
            chat_response=FakeResponse(status_code=500, json_data={}),
        )

        with patch("dendrite_scraper.scraper.httpx.AsyncClient", return_value=client):
            assert await emberlm_clean_markdown("# Hi\n") is None


class TestCleanupProvider:
    """Tests for cleanup provider selection."""

    def test_auto_prefers_emberlm_when_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "cleanup_provider", "auto")
        monkeypatch.setattr(scraper_module.settings, "emberlm_provider", "mlx")
        monkeypatch.setattr(scraper_module.settings, "emberlm_model", "local-model")

        assert resolve_cleanup_provider() == "emberlm"

    def test_auto_without_emberlm_disables_cleanup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "cleanup_provider", "auto")
        monkeypatch.setattr(scraper_module.settings, "emberlm_provider", None)
        monkeypatch.setattr(scraper_module.settings, "emberlm_model", None)

        assert resolve_cleanup_provider() == "none"

    def test_none_disables_cleanup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "cleanup_provider", "none")
        monkeypatch.setattr(scraper_module.settings, "emberlm_provider", "mlx")
        monkeypatch.setattr(scraper_module.settings, "emberlm_model", "local-model")

        assert resolve_cleanup_provider() == "none"

    @pytest.mark.asyncio
    async def test_llm_clean_dispatches_to_emberlm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "cleanup_provider", "emberlm")
        monkeypatch.setattr(scraper_module.settings, "emberlm_provider", "mlx")
        monkeypatch.setattr(scraper_module.settings, "emberlm_model", "local-model")

        with patch(
            "dendrite_scraper.scraper.emberlm_clean_markdown",
            new=AsyncMock(return_value="# Local\n"),
        ):
            assert await llm_clean_markdown("# Raw\n") == "# Local\n"


# ── maybe_llm_clean ──────────────────────────────────────────


class TestMaybeLlmClean:
    """Tests for the conditional LLM cleanup gate."""

    @pytest.mark.asyncio
    async def test_non_noisy_skips_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "cleanup_provider", "emberlm")
        monkeypatch.setattr(scraper_module.settings, "emberlm_provider", "mlx")
        monkeypatch.setattr(scraper_module.settings, "emberlm_model", "local-model")

        with patch(
            "dendrite_scraper.scraper.llm_clean_markdown", new_callable=AsyncMock
        ) as mock_clean:
            cleaned, used_llm = await maybe_llm_clean("plain content")

        assert cleaned == "plain content"
        assert used_llm is False
        mock_clean.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_provider_skips_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "cleanup_provider", "none")
        noisy = "\n".join(f"[Link {i}](https://example.com/{i})" for i in range(20))

        with patch(
            "dendrite_scraper.scraper.llm_clean_markdown", new_callable=AsyncMock
        ) as mock_clean:
            cleaned, used_llm = await maybe_llm_clean(noisy)

        assert cleaned == noisy
        assert used_llm is False
        mock_clean.assert_not_called()

    @pytest.mark.asyncio
    async def test_noisy_content_uses_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "cleanup_provider", "emberlm")
        monkeypatch.setattr(scraper_module.settings, "emberlm_provider", "mlx")
        monkeypatch.setattr(scraper_module.settings, "emberlm_model", "local-model")
        noisy = "\n".join(f"[Link {i}](https://example.com/{i})" for i in range(20))

        with patch(
            "dendrite_scraper.scraper.llm_clean_markdown",
            new=AsyncMock(return_value="# Clean\n"),
        ):
            cleaned, used_llm = await maybe_llm_clean(noisy)

        assert cleaned == "# Clean\n"
        assert used_llm is True

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_original(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scraper_module.settings, "cleanup_provider", "emberlm")
        monkeypatch.setattr(scraper_module.settings, "emberlm_provider", "mlx")
        monkeypatch.setattr(scraper_module.settings, "emberlm_model", "local-model")
        noisy = "\n".join(f"[Link {i}](https://example.com/{i})" for i in range(20))

        with patch("dendrite_scraper.scraper.llm_clean_markdown", new=AsyncMock(return_value=None)):
            cleaned, used_llm = await maybe_llm_clean(noisy)

        assert cleaned == noisy
        assert used_llm is False


# ── scrape orchestration ─────────────────────────────────────


class TestScrape:
    """Tests for the top-level scrape pipeline."""

    @pytest.mark.asyncio
    async def test_crawl4ai_success(self) -> None:
        with (
            patch(
                "dendrite_scraper.scraper.crawl_url",
                new=AsyncMock(return_value=("# Title\n", False)),
            ),
            patch("dendrite_scraper.scraper.jina_fetch", new=AsyncMock()) as mock_jina,
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
                "dendrite_scraper.scraper.crawl_url",
                new=AsyncMock(side_effect=[("transient failure", True), ("# Final\n", False)]),
            ),
            patch("dendrite_scraper.scraper.asyncio.sleep", new=AsyncMock()) as mock_sleep,
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
                "dendrite_scraper.scraper.crawl_url",
                new=AsyncMock(return_value=("Just a moment", False)),
            ),
            patch(
                "dendrite_scraper.scraper.jina_fetch",
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
                "dendrite_scraper.scraper.crawl_url",
                new=AsyncMock(return_value=("Crawl timed out after 25s", True)),
            ) as mock_crawl,
            patch(
                "dendrite_scraper.scraper.jina_fetch",
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
                "dendrite_scraper.scraper.crawl_url",
                new=AsyncMock(side_effect=[RuntimeError("boom 1"), RuntimeError("boom 2")]),
            ),
            patch(
                "dendrite_scraper.scraper.jina_fetch",
                new=AsyncMock(return_value=("Jina Reader failed: nope", True)),
            ),
            patch("dendrite_scraper.scraper.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            result = await scrape("https://example.com")

        assert result.source == "none"
        assert result.error == "Scrape crashed: boom 2"
        assert result.attempts == ["crawl4ai attempt 1", "crawl4ai attempt 2", "jina fallback"]
        mock_sleep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_jina_success_can_mark_llm_cleaned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scraper_module.settings, "jina_enabled", True)
        noisy = "\n".join(f"[Link {i}](https://example.com/{i})" for i in range(20))

        with (
            patch(
                "dendrite_scraper.scraper.crawl_url",
                new=AsyncMock(return_value=("Just a moment", False)),
            ),
            patch(
                "dendrite_scraper.scraper.jina_fetch", new=AsyncMock(return_value=(noisy, False))
            ),
            patch(
                "dendrite_scraper.scraper.maybe_llm_clean",
                new=AsyncMock(return_value=("# Clean\n", True)),
            ),
        ):
            result = await scrape("https://example.com")

        assert result.source == "jina"
        assert result.llm_cleaned is True
        assert result.markdown == "# Clean\n"


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
            patch("dendrite_scraper.scraper.crawl_url", new=AsyncMock()) as mock_crawl,
            patch("dendrite_scraper.scraper.jina_fetch", new=AsyncMock()) as mock_jina,
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
                "dendrite_scraper.scraper.crawl_url",
                new=AsyncMock(return_value=("Just a moment", False)),
            ),
            patch("dendrite_scraper.scraper.jina_fetch", new=AsyncMock()) as mock_jina,
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

        with patch("dendrite_scraper.scraper.httpx.AsyncClient", return_value=client):
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
        with patch("dendrite_scraper.scraper.httpx.AsyncClient", return_value=client):
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
        with patch("dendrite_scraper.scraper.httpx.AsyncClient", return_value=client):
            message, is_error = await jina_fetch("https://example.com")

        assert is_error is True
        assert "too large" in message

    @pytest.mark.asyncio
    async def test_route_guard_aborts_after_max_redirects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dendrite_scraper.scraper import _make_route_guard

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
