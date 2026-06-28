"""Application settings resolved from environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Dendrite scraper service configuration.

    @param jina_enabled: Opt-in flag for the third-party Jina Reader fallback (default off).
    @param api_key: Optional API key; when set, POST /scrape requires a matching key.
    @param host: Bind address for the HTTP server (127.0.0.1 = local only; set 0.0.0.0 to expose).
    @param port: Bind port for the HTTP server.
    @param server_timeout_seconds: Global per-request deadline for POST /scrape.
    @param max_concurrent_scrapes: Maximum concurrent in-flight scrapes (bounds Chromium/memory).
    @param scrape_acquire_timeout_seconds: How long a request waits for a concurrency slot before 503.
    @param crawl_timeout_seconds: Per-URL crawl4ai timeout.
    @param jina_timeout_seconds: Per-URL Jina Reader timeout.
    @param jina_max_bytes: Reject a Jina response whose declared length exceeds this.
    @param max_markdown_chars: Cap on scraped markdown length before heuristics/cleaning.
    @param max_retries: Crawl4AI retry attempts on transient errors.
    @param retry_delay_seconds: Delay between retries.
    @param max_redirects: Maximum redirect hops the browser may follow per crawl.
    @param crawl_render_delay_seconds: Fixed delay before crawl4ai captures HTML,
        letting JS render dynamic content. 0 (default) for static targets; raise
        for JS-heavy SPAs. Adds this much latency to every crawl.
    @param crawl_page_timeout_ms: Per-page Playwright navigation timeout, in ms.
    @param crawl_word_count_threshold: crawl4ai minimum words per content block.
    """

    jina_enabled: bool = False
    api_key: str | None = None
    host: str = "127.0.0.1"
    port: int = Field(8020, ge=1, le=65535)
    server_timeout_seconds: float = Field(120.0, ge=0)
    max_concurrent_scrapes: int = Field(4, ge=1)
    scrape_acquire_timeout_seconds: float = Field(5.0, ge=0)
    crawl_timeout_seconds: float = Field(25.0, ge=0)
    jina_timeout_seconds: float = Field(30.0, ge=0)
    jina_max_bytes: int = Field(5_000_000, ge=1)
    max_markdown_chars: int = Field(1_000_000, ge=1)
    max_retries: int = Field(2, ge=0)
    retry_delay_seconds: float = Field(1.0, ge=0)
    max_redirects: int = Field(5, ge=0)
    crawl_render_delay_seconds: float = Field(0.0, ge=0)
    crawl_page_timeout_ms: int = Field(15000, ge=1)
    crawl_word_count_threshold: int = Field(10, ge=1)
    # Inbound request body cap (F5): Starlette has no built-in limit, so without
    # this a caller can POST an unbounded JSON body. 1 MiB is generous for a
    # {"url": "..."} payload while blocking memory-exhaustion attempts.
    max_request_body_bytes: int = Field(1_048_576, ge=1)

    model_config = {"env_prefix": "SCRAPER_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
