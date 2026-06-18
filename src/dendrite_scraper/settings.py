"""Application settings resolved from environment variables.

Cleanup is optional. It uses EmberLM-warmed local models when noisy scraped
markdown needs a stronger content-extraction pass.
"""

from typing import Literal

from pydantic_settings import BaseSettings

CleanupProvider = Literal["auto", "none", "emberlm"]


class Settings(BaseSettings):
    """Dendrite scraper service configuration.

    @param jina_enabled: Opt-in flag for the third-party Jina Reader fallback (default off).
    @param api_key: Optional API key; when set, POST /scrape requires a matching key.
    @param cleanup_provider: Cleanup backend: auto, none, or emberlm.
    @param emberlm_provider: EmberLM provider used for local cleanup (e.g. mlx, ollama).
    @param emberlm_model: EmberLM model used for local cleanup.
    @param emberlm_app_name: App name sent to EmberLM (warm appName + X-EmberLM-App header).
    @param emberlm_daemon_url: EmberLM daemon base URL for the warm call.
    @param emberlm_warm_timeout_seconds: Timeout for the EmberLM warm call (blocks until ready).
    @param emberlm_keep_warm_ms: Optional keep-warm hint passed to EmberLM warm.
    @param host: Bind address for the HTTP server (127.0.0.1 = local only; set 0.0.0.0 to expose).
    @param port: Bind port for the HTTP server.
    @param server_timeout_seconds: Global per-request deadline for POST /scrape.
    @param max_concurrent_scrapes: Maximum concurrent in-flight scrapes (bounds Chromium/memory).
    @param scrape_acquire_timeout_seconds: How long a request waits for a concurrency slot before 503.
    @param crawl_timeout_seconds: Per-URL crawl4ai timeout.
    @param jina_timeout_seconds: Per-URL Jina Reader timeout.
    @param jina_max_bytes: Reject a Jina response whose declared length exceeds this.
    @param max_markdown_chars: Cap on scraped markdown length before heuristics/cleaning.
    @param llm_clean_timeout_seconds: Cleanup model request timeout.
    @param llm_clean_max_input_chars: Truncation limit for LLM cleanup input.
    @param max_retries: Crawl4AI retry attempts on transient errors.
    @param retry_delay_seconds: Delay between retries.
    @param max_redirects: Maximum redirect hops the browser may follow per crawl.
    """

    jina_enabled: bool = False
    api_key: str | None = None
    cleanup_provider: CleanupProvider = "auto"
    emberlm_provider: str | None = "mlx"
    emberlm_model: str | None = "mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"
    emberlm_app_name: str = "dendrite-scraper"
    emberlm_daemon_url: str = "http://127.0.0.1:17412"
    emberlm_warm_timeout_seconds: float = 180.0
    emberlm_keep_warm_ms: int | None = None
    host: str = "127.0.0.1"
    port: int = 8020
    server_timeout_seconds: int = 120
    max_concurrent_scrapes: int = 4
    scrape_acquire_timeout_seconds: float = 5.0
    crawl_timeout_seconds: int = 25
    jina_timeout_seconds: int = 30
    jina_max_bytes: int = 5_000_000
    max_markdown_chars: int = 1_000_000
    llm_clean_timeout_seconds: int = 90
    llm_clean_max_input_chars: int = 80_000
    max_retries: int = 2
    retry_delay_seconds: float = 1.0
    max_redirects: int = 5

    model_config = {"env_prefix": "DENDRITE_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
