"""Tests for security-relevant settings defaults."""

import pytest
from pydantic import ValidationError

from scraper.settings import Settings


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

    @pytest.mark.parametrize(
        ("field", "bad"),
        [
            ("port", 0),
            ("port", 70000),
            ("max_concurrent_scrapes", 0),
            ("max_retries", -1),
            ("max_redirects", -1),
            ("crawl_timeout_seconds", -1),
            ("server_timeout_seconds", -1),
            ("jina_max_bytes", 0),
            ("max_markdown_chars", 0),
            ("max_request_body_bytes", 0),
        ],
    )
    def test_out_of_bounds_rejected(self, field: str, bad: object) -> None:
        with pytest.raises(ValidationError):
            Settings.model_validate({field: bad})

    def test_zero_timeout_allowed(self) -> None:
        # Zero is a valid "disable" floor for timeouts (immediate deadline),
        # unlike negative values.
        assert Settings.model_validate({"crawl_timeout_seconds": 0}).crawl_timeout_seconds == 0
