"""Tests for the HTTP server endpoints.

Uses FastAPI's TestClient — no real server needed.
Scraping calls are mocked to avoid network access.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import dendrite_scraper.server as server_module
from dendrite_scraper.scraper import ScrapeResult
from dendrite_scraper.settings import settings


class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_returns_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_response_shape_is_stable(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert set(resp.json().keys()) == {"status"}


class TestScrapeEndpoint:
    """Tests for POST /scrape."""

    @patch("dendrite_scraper.server.scrape", new_callable=AsyncMock)
    def test_successful_scrape(self, mock_scrape: AsyncMock, client: TestClient) -> None:
        mock_scrape.return_value = ScrapeResult(
            markdown="# Hello\n\nWorld\n",
            source="crawl4ai",
            url="https://example.com",
            elapsed_ms=150.0,
            attempts=["crawl4ai attempt 1"],
        )

        resp = client.post("/scrape", json={"url": "https://example.com"})
        assert resp.status_code == 200

        body = resp.json()
        assert set(body.keys()) == {
            "markdown",
            "source",
            "url",
            "bot_detected",
            "llm_cleaned",
            "error",
            "elapsed_ms",
            "attempts",
        }
        assert body["markdown"] == "# Hello\n\nWorld\n"
        assert body["source"] == "crawl4ai"
        assert body["url"] == "https://example.com"
        assert body["bot_detected"] is False
        assert body["llm_cleaned"] is False
        assert body["error"] is None
        assert body["elapsed_ms"] == 150.0
        assert body["attempts"] == ["crawl4ai attempt 1"]

    @patch("dendrite_scraper.server.scrape", new_callable=AsyncMock)
    def test_failed_scrape(self, mock_scrape: AsyncMock, client: TestClient) -> None:
        mock_scrape.return_value = ScrapeResult(
            url="https://example.com",
            error="Both crawl4ai and Jina failed",
            elapsed_ms=5000.0,
            attempts=["crawl4ai attempt 1", "crawl4ai attempt 2", "jina fallback"],
        )

        resp = client.post("/scrape", json={"url": "https://example.com"})
        assert resp.status_code == 200

        body = resp.json()
        assert set(body.keys()) == {
            "markdown",
            "source",
            "url",
            "bot_detected",
            "llm_cleaned",
            "error",
            "elapsed_ms",
            "attempts",
        }
        assert body["markdown"] == ""
        assert body["source"] == "none"
        assert body["url"] == "https://example.com"
        assert body["bot_detected"] is False
        assert body["llm_cleaned"] is False
        assert body["error"] == "Both crawl4ai and Jina failed"
        assert body["elapsed_ms"] == 5000.0
        assert body["attempts"] == ["crawl4ai attempt 1", "crawl4ai attempt 2", "jina fallback"]

    @patch("dendrite_scraper.server.scrape", new_callable=AsyncMock)
    def test_bot_detected_with_jina_fallback(
        self, mock_scrape: AsyncMock, client: TestClient
    ) -> None:
        mock_scrape.return_value = ScrapeResult(
            markdown="# Real content from Jina\n",
            source="jina",
            url="https://protected.example.com",
            bot_detected=True,
            elapsed_ms=3200.0,
            attempts=["crawl4ai attempt 1", "bot protection detected", "jina fallback"],
        )

        resp = client.post("/scrape", json={"url": "https://protected.example.com"})
        assert resp.status_code == 200

        body = resp.json()
        assert body["source"] == "jina"
        assert body["bot_detected"] is True
        assert body["markdown"] != ""

    def test_invalid_url_rejected(self, client: TestClient) -> None:
        resp = client.post("/scrape", json={"url": "not-a-url"})
        assert resp.status_code == 422

    def test_missing_url_rejected(self, client: TestClient) -> None:
        resp = client.post("/scrape", json={})
        assert resp.status_code == 422

    def test_internal_ip_rejected_400(self, client: TestClient) -> None:
        resp = client.post("/scrape", json={"url": "http://127.0.0.1:8020/health"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "URL rejected"

    def test_metadata_endpoint_rejected_400(self, client: TestClient) -> None:
        resp = client.post("/scrape", json={"url": "http://169.254.169.254/latest/meta-data/"})
        assert resp.status_code == 400


class TestServerResourceLimits:
    """Tests for the global timeout and concurrency cap (M6, M7)."""

    def test_global_timeout_returns_error(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "server_timeout_seconds", 0.05)

        async def _hang(_url: str) -> ScrapeResult:
            await asyncio.sleep(5)
            return ScrapeResult()  # pragma: no cover

        with patch("dendrite_scraper.server.scrape", new=_hang):
            resp = client.post("/scrape", json={"url": "https://example.com"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] is not None
        assert "timeout" in body["error"].lower()

    def test_over_capacity_returns_503(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(server_module, "_scrape_semaphore", asyncio.Semaphore(0))
        monkeypatch.setattr(settings, "scrape_acquire_timeout_seconds", 0.05)

        resp = client.post("/scrape", json={"url": "https://example.com"})
        assert resp.status_code == 503
        assert resp.headers.get("Retry-After") == "5"


class TestApiKeyAuth:
    """Tests for the optional API key (M8)."""

    @patch("dendrite_scraper.server.scrape", new_callable=AsyncMock)
    def test_open_when_unset(self, mock_scrape: AsyncMock, client: TestClient) -> None:
        mock_scrape.return_value = ScrapeResult(url="https://example.com", source="crawl4ai")
        resp = client.post("/scrape", json={"url": "https://example.com"})
        assert resp.status_code == 200

    def test_missing_key_rejected_when_set(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "api_key", "s3cret")
        resp = client.post("/scrape", json={"url": "https://example.com"})
        assert resp.status_code == 401

    def test_wrong_key_rejected(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "api_key", "s3cret")
        resp = client.post(
            "/scrape", json={"url": "https://example.com"}, headers={"X-API-Key": "nope"}
        )
        assert resp.status_code == 401

    @patch("dendrite_scraper.server.scrape", new_callable=AsyncMock)
    def test_correct_x_api_key_accepted(
        self, mock_scrape: AsyncMock, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "api_key", "s3cret")
        mock_scrape.return_value = ScrapeResult(url="https://example.com", source="crawl4ai")
        resp = client.post(
            "/scrape", json={"url": "https://example.com"}, headers={"X-API-Key": "s3cret"}
        )
        assert resp.status_code == 200

    @patch("dendrite_scraper.server.scrape", new_callable=AsyncMock)
    def test_correct_bearer_token_accepted(
        self, mock_scrape: AsyncMock, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "api_key", "s3cret")
        mock_scrape.return_value = ScrapeResult(url="https://example.com", source="crawl4ai")
        resp = client.post(
            "/scrape",
            json={"url": "https://example.com"},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert resp.status_code == 200
