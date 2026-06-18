"""Unit tests for the SSRF safety guard.

Resolution is injected, so these tests never touch DNS or the network.
"""

import pytest

from dendrite_scraper.safety import (
    DEFAULT_MAX_REDIRECTS,
    UrlRejected,
    ValidatedTarget,
    classify_ip,
    pin_request_kwargs,
    revalidate_redirect,
    validate_url,
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
        ],
    )
    def test_internal_addresses_blocked(self, ip: str) -> None:
        assert classify_ip(ip) is not None

    @pytest.mark.parametrize("ip", ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"])
    def test_public_addresses_allowed(self, ip: str) -> None:
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
        "scheme_url", ["ftp://example.com", "file:///etc/passwd", "gopher://x"]
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


# ── revalidate_redirect ──────────────────────────────────────


class TestRevalidateRedirect:
    """Tests for per-hop redirect validation."""

    def test_internal_location_rejected(self) -> None:
        with pytest.raises(UrlRejected):
            revalidate_redirect(
                "http://169.254.169.254/latest/meta-data/",
                hop=1,
                resolver=_resolver("169.254.169.254"),
            )

    def test_public_location_allowed(self) -> None:
        target = revalidate_redirect(
            "https://example.org/next", hop=2, resolver=_resolver("93.184.216.34")
        )
        assert target.host == "example.org"

    def test_hop_count_enforced(self) -> None:
        with pytest.raises(UrlRejected) as exc:
            revalidate_redirect(
                "https://example.org", hop=DEFAULT_MAX_REDIRECTS + 1, resolver=_resolver("1.1.1.1")
            )
        assert "too-many-redirects" in exc.value.reason


# ── pin_request_kwargs ───────────────────────────────────────


class TestPinRequestKwargs:
    """Tests for the IP-pinning request rewrite."""

    def test_pins_to_validated_ip_preserving_host_and_sni(self) -> None:
        target = validate_url("https://example.com/p?q=1", resolver=_resolver("93.184.216.34"))
        kwargs = pin_request_kwargs(target)
        assert kwargs["url"] == "https://93.184.216.34/p?q=1"
        assert kwargs["headers"] == {"Host": "example.com"}
        assert kwargs["extensions"] == {"sni_hostname": "example.com"}
