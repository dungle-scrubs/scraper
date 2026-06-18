"""Shared fixtures for dendrite-scraper tests."""

import ipaddress

import pytest
from fastapi.testclient import TestClient

from dendrite_scraper.server import app


@pytest.fixture
def client() -> TestClient:
    """Synchronous test client for the FastAPI app."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the SSRF guard's DNS resolution hermetic and deterministic.

    IP literals resolve to themselves (so internal-IP rejection still works);
    every hostname resolves to a fixed public IP (so example.com validates as
    public without touching the network).
    """

    def fake_resolver(host: str) -> list[str]:
        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            return ["93.184.216.34"]

    monkeypatch.setattr("dendrite_scraper.safety._default_resolver", fake_resolver)
