"""Unit tests for the SSRF safety guard.

Resolution is injected, so these tests never touch DNS or the network.
"""

import pytest

from scraper.safety import (
    UrlRejected,
    ValidatedTarget,
    classify_ip,
    validate_url,
    validate_url_async,
)


def _resolver(*addresses: str):
    """Build a fixed resolver returning the given addresses for any host."""

    def resolve(_host: str) -> list[str]:
        return list(addresses)

    return resolve


# ── classify_ip ──────────────────────────────────────────────


class TestClassifyIp:
    """Tests for IP classification."""

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",  # loopback v4
            "::1",  # loopback v6
            "10.0.0.5",  # private
            "172.16.0.1",  # private
            "192.168.1.1",  # private
            "fc00::1",  # ULA
            "169.254.0.1",  # link-local
            "169.254.169.254",  # cloud metadata (link-local)
            "fe80::1",  # link-local v6
            "100.64.0.1",  # CGNAT / shared address space
            "0.0.0.0",  # unspecified
            "::",  # unspecified v6
            "224.0.0.1",  # multicast
            "::ffff:127.0.0.1",  # IPv4-mapped loopback (bypass attempt)
            # IPv6 encodings of internal IPv4 that the mapped-only unwrap misses.
            "::127.0.0.1",  # IPv4-compatible loopback
            "::10.0.0.1",  # IPv4-compatible private
            "::169.254.169.254",  # IPv4-compatible metadata
            "64:ff9b::127.0.0.1",  # NAT64 loopback
            "64:ff9b::169.254.169.254",  # NAT64 metadata
        ],
    )
    def test_internal_addresses_blocked(self, ip: str) -> None:
        assert classify_ip(ip) is not None

    @pytest.mark.parametrize("ip", ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"])
    def test_public_addresses_allowed(self, ip: str) -> None:
        assert classify_ip(ip) is None

    @pytest.mark.parametrize(
        "ip",
        [
            # Public IPv4 wrapped in the IPv4-mapped v6 encoding must still be ALLOWED.
            "::ffff:93.184.216.34",  # mapped public
        ],
    )
    def test_embedded_public_ipv4_allowed(self, ip: str) -> None:
        assert classify_ip(ip) is None

    def test_unparseable_blocked(self) -> None:
        assert classify_ip("not-an-ip") == "unparseable-ip"

    def test_metadata_reason_is_link_local(self) -> None:
        assert classify_ip("169.254.169.254") == "link-local"


# ── validate_url ─────────────────────────────────────────────


class TestValidateUrl:
    """Tests for URL validation."""

    def test_public_host_accepted(self) -> None:
        target = validate_url("https://example.com/docs", resolver=_resolver("93.184.216.34"))
        assert isinstance(target, ValidatedTarget)
        assert target.url == "https://example.com/docs"
        assert target.host == "example.com"
        assert target.addresses == ("93.184.216.34",)

    @pytest.mark.parametrize(
        "scheme_url",
        [
            "ftp://example.com",
            "file:///etc/passwd",
            "gopher://x",
            # Additional non-http(s) schemes that must never reach a fetcher.
            "data:text/html,<script>alert(1)</script>",
            "blob:https://example.com/xxxxxxxx-xxxx",
            "javascript:alert(1)",
        ],
    )
    def test_non_http_scheme_rejected(self, scheme_url: str) -> None:
        with pytest.raises(UrlRejected) as exc:
            validate_url(scheme_url, resolver=_resolver("93.184.216.34"))
        assert "scheme-not-allowed" in exc.value.reason

    def test_strips_userinfo(self) -> None:
        target = validate_url(
            "https://user:pass@example.com/x", resolver=_resolver("93.184.216.34")
        )
        assert "user" not in target.url
        assert "pass" not in target.url
        assert target.url == "https://example.com/x"

    def test_rejects_when_any_resolved_address_internal(self) -> None:
        # A public-looking host that resolves to an internal IP (DNS rebinding shape).
        with pytest.raises(UrlRejected) as exc:
            validate_url("https://sneaky.example.com", resolver=_resolver("127.0.0.1"))
        assert "blocked-address" in exc.value.reason

    def test_rejects_when_one_of_many_is_internal(self) -> None:
        with pytest.raises(UrlRejected):
            validate_url(
                "https://multi.example.com", resolver=_resolver("93.184.216.34", "10.0.0.1")
            )

    def test_ip_literal_internal_rejected_without_resolver(self) -> None:
        with pytest.raises(UrlRejected):
            validate_url("http://127.0.0.1:8020/admin", resolver=_resolver("203.0.113.9"))

    def test_blocked_hostname_denylist(self) -> None:
        with pytest.raises(UrlRejected) as exc:
            validate_url("http://metadata.google.internal/x", resolver=_resolver("93.184.216.34"))
        assert "blocked-hostname" in exc.value.reason

    def test_missing_host_rejected(self) -> None:
        with pytest.raises(UrlRejected):
            validate_url("http:///just/a/path", resolver=_resolver("93.184.216.34"))

    def test_ipv6_literal_public_accepted(self) -> None:
        target = validate_url("https://[2606:4700:4700::1111]/", resolver=_resolver())
        assert target.host == "2606:4700:4700::1111"

    def test_port_zero_rejected(self) -> None:
        # Port 0 parses but is not a connectable port; reject it.
        with pytest.raises(UrlRejected) as exc:
            validate_url("http://example.com:0/", resolver=_resolver("93.184.216.34"))
        assert "invalid-port" in exc.value.reason


# ── validate_url_async ───────────────────────────────────────


class TestValidateUrlAsync:
    """Tests for the async (thread-offloaded) validator."""

    @pytest.mark.asyncio
    async def test_public_host_accepted(self) -> None:
        target = await validate_url_async(
            "https://example.com/docs", resolver=_resolver("93.184.216.34")
        )
        assert isinstance(target, ValidatedTarget)
        assert target.host == "example.com"

    @pytest.mark.asyncio
    async def test_internal_rejected(self) -> None:
        with pytest.raises(UrlRejected):
            await validate_url_async("http://10.0.0.5/", resolver=_resolver("10.0.0.5"))
