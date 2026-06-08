"""Application settings resolved from environment variables.

Cleanup is optional. It uses Hector-backed local models when noisy scraped
markdown needs a stronger content-extraction pass.
"""

from typing import Literal

from pydantic_settings import BaseSettings

CleanupProvider = Literal["auto", "none", "hector"]


class Settings(BaseSettings):
    """Dendrite scraper service configuration.

    @param cleanup_provider: Cleanup backend: auto, none, or hector.
    @param hector_provider: Hector provider used for local cleanup.
    @param hector_model: Hector model used for local cleanup.
    @param hector_app_name: App name used for Hector leases.
    @param hector_timeout_seconds: Hector ensure/chat timeout.
    @param hector_sweep_on_exit: Whether to request sweep when releasing a Hector lease.
    @param host: Bind address for the HTTP server.
    @param port: Bind port for the HTTP server.
    @param crawl_timeout_seconds: Per-URL crawl4ai timeout.
    @param jina_timeout_seconds: Per-URL Jina Reader timeout.
    @param llm_clean_timeout_seconds: Cleanup model request timeout.
    @param llm_clean_max_input_chars: Truncation limit for LLM cleanup input.
    @param max_retries: Crawl4AI retry attempts on transient errors.
    @param retry_delay_seconds: Delay between retries.
    """

    cleanup_provider: CleanupProvider = "auto"
    hector_provider: str | None = "mlx"
    hector_model: str | None = "mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"
    hector_app_name: str = "dendrite-scraper"
    hector_timeout_seconds: float = 180.0
    hector_sweep_on_exit: bool = False
    host: str = "0.0.0.0"
    port: int = 8020
    crawl_timeout_seconds: int = 25
    jina_timeout_seconds: int = 30
    llm_clean_timeout_seconds: int = 90
    llm_clean_max_input_chars: int = 80_000
    max_retries: int = 2
    retry_delay_seconds: float = 1.0

    model_config = {"env_prefix": "DENDRITE_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
