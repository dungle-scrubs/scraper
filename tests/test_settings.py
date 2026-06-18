"""Tests for security-relevant settings defaults."""

from dendrite_scraper.settings import Settings


class TestSecurityDefaults:
    """The shipped defaults must be safe-by-default for local use."""

    def test_host_defaults_to_localhost(self) -> None:
        assert Settings.model_fields["host"].default == "127.0.0.1"

    def test_jina_disabled_by_default(self) -> None:
        assert Settings.model_fields["jina_enabled"].default is False

    def test_api_key_unset_by_default(self) -> None:
        assert Settings.model_fields["api_key"].default is None

    def test_global_server_timeout_present(self) -> None:
        assert Settings.model_fields["server_timeout_seconds"].default == 120

    def test_concurrency_cap_present(self) -> None:
        assert Settings.model_fields["max_concurrent_scrapes"].default >= 1
