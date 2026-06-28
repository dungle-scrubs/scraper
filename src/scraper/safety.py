"""URL safety guard — the single SSRF trust boundary for scraper.

Every URL that reaches a fetch path (crawl4ai, Jina, the CLI) must pass through
`validate_url` before any network egress, and every in-browser request/redirect
hop is re-validated by the caller's route guard (see scraper._guard_route). This
module owns:

  - scheme restriction (http/https only),
  - credential stripping (no `user:pass@` ever leaves the process),
  - host resolution + IP classification (block loopback, private, link-local,
    reserved, multicast, unspecified, CGNAT/shared, and cloud-metadata targets),
  - async (thread-offloaded) validation so DNS can't block the event loop.

It owns the *decision* to allow or block a target. It does NOT perform the
fetch itself — callers do, using the `ValidatedTarget` it returns. Resolution is
injectable (`resolver`) so the policy is unit-testable without DNS or network.

Design caveats (inherent to this architecture, not fully closeable here):
  - TOCTOU: `validate_url` resolves the host and classifies the IPs, but the
    actual fetcher (crawl4ai/Playwright, Jina's httpx) re-resolves
    independently. A window remains between validate-time and connect-time
    resolution (classic DNS rebinding). The per-hop re-validation in the route
    guard narrows but does not eliminate it. Fully closing it would require a
    custom resolver→connect path (e.g. a SOCKS/proxy that pins the validated IP),
    which is out of scope for a local tool.
  - Re-resolution: `ValidatedTarget.addresses` are the validated IPs, but
    callers fetch the *hostname* (so TLS SNI/Host headers are correct), not the
    IPs. This is intentional and correct for HTTPS, and is the source of the
    TOCTOU window above.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

# ── Constants ────────────────────────────────────────────────

ALLOWED_SCHEMES = ("http", "https")

# Resolved-IP classification is the primary control: metadata.google.internal and
# localhost resolve to link-local / loopback addresses and are blocked there. This
# name denylist is defense-in-depth for hosts that might resolve inconsistently.
BLOCKED_HOSTNAMES = frozenset({"metadata.google.internal"})

Resolver = Callable[[str], list[str]]


# ── Errors ───────────────────────────────────────────────────


class UrlRejected(ValueError):
    """Raised when a URL or redirect target fails the SSRF safety guard.

    @param reason: Short machine-friendly reason (safe to log, not to reflect).
    @param url: The offending URL, when available.
    @param host: The offending host, when available.
    """

    def __init__(self, reason: str, *, url: str = "", host: str = "") -> None:
        self.reason = reason
        self.url = url
        self.host = host
        super().__init__(reason)


# ── Result types ─────────────────────────────────────────────


@dataclass(frozen=True)
class ValidatedTarget:
    """A URL that passed the guard, with its validated resolved addresses.

    @param url: Sanitized URL (userinfo stripped), safe to fetch or forward.
    @param scheme: Lowercased scheme (http or https).
    @param host: Original hostname, preserved for Host header / TLS SNI.
    @param port: Explicit port if present, else None.
    @param addresses: Resolved IP literals that were all classified public.
    """

    url: str
    scheme: str
    host: str
    port: int | None
    addresses: tuple[str, ...]


# ── IP classification ────────────────────────────────────────


def _embedded_ipv4(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    """Extract an embedded IPv4 from an IPv6 mapping/translation prefix.

    Handles the three encodings that embed an IPv4 address in a v6 literal:
      - IPv4-mapped   ::ffff:a.b.c.d   (RFC 4291) — also `.ipv4_mapped`
      - IPv4-compatible ::a.b.c.d       (RFC 4291, deprecated) — NOT `.ipv4_mapped`
      - NAT64/WKP      64:ff9b::a.b.c.d (RFC 6052)        — NOT `.ipv4_mapped`

    6to4 (2002::/16) and Teredo (2001::/32) are intentionally NOT handled here:
    they report `is_global=False`/`is_private` in CPython and are caught by the
    generic checks below.

    @param addr: Parsed IP address (v4 or v6).
    @returns: The embedded IPv4 address for the encodings above, else None.
    """
    if not isinstance(addr, ipaddress.IPv6Address):
        return None

    raw = int(addr)
    high64 = raw >> 64
    mid32 = (raw >> 32) & 0xFFFFFFFF
    low64 = raw & ((1 << 64) - 1)
    low32 = raw & 0xFFFFFFFF

    # IPv4-mapped ::ffff:0:0/96
    if high64 == 0 and mid32 == 0xFFFF:
        return ipaddress.IPv4Address(low32)
    # NAT64 Well-Known Prefix 64:ff9b::/96
    if high64 == 0x0064FF9B00000000 and low64 >> 32 == 0:
        return ipaddress.IPv4Address(low32)
    # IPv4-compatible ::a.b.c.d (the deprecated ::/96 form, excluding :: and ::1).
    if high64 == 0 and mid32 == 0 and low64 >> 32 == 0 and low32 not in (0, 1):
        return ipaddress.IPv4Address(low32)

    return None


def classify_ip(ip: str) -> str | None:
    """Return a block reason for an internal/special address, or None if public.

    @param ip: IP address literal (v4 or v6).
    @returns: Reason string when the address must be blocked, else None.
    """
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(ip)
    except ValueError:
        return "unparseable-ip"

    # Unwrap IPv4 embedded in IPv6, so ::ffff:127.0.0.1, ::127.0.0.1 (compat),
    # and 64:ff9b::127.0.0.1 (NAT64) can't encode a private/loopback/link-local
    # address that the v6-only checks would miss. The stdlib `.ipv4_mapped`
    # unwrap covers ONLY the ::ffff:0:0/96 mapped form, so compat (::a.b.c.d)
    # and NAT64 (64:ff9b::/96) — which report is_global=True even when the
    # embedded v4 is private — are handled explicitly here.
    embedded = _embedded_ipv4(addr)
    if embedded is not None:
        reason = classify_ip(str(embedded))
        if reason is not None:
            return f"embedded-ipv4: {reason}"

    if addr.is_unspecified:
        return "unspecified"
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:  # 169.254/16 (incl. 169.254.169.254), fe80::/10
        return "link-local"
    if addr.is_multicast:
        return "multicast"
    if addr.is_reserved:
        return "reserved"
    if addr.is_private:  # 10/8, 172.16/12, 192.168/16, fc00::/7 (ULA), CGNAT on 3.12.4+
        return "private"
    # Belt-and-suspenders: anything not globally routable unicast (e.g. CGNAT on
    # older interpreters, documentation ranges) is blocked.
    if not addr.is_global:
        return "non-global"
    return None


# ── Resolution ───────────────────────────────────────────────


def _default_resolver(host: str) -> list[str]:
    """Resolve a hostname to its A/AAAA address literals via the system resolver.

    @param host: Hostname or IP literal.
    @returns: Deduplicated list of resolved IP literals.
    @throws UrlRejected: When the host fails to resolve.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UrlRejected(f"dns-resolution-failed: {host}", host=host) from exc
    return list({str(info[4][0]) for info in infos})


def _format_netloc(host: str, port: int | None) -> str:
    """Rebuild a netloc from host + port, bracketing IPv6 literals.

    @param host: Hostname or IP literal (no brackets).
    @param port: Optional port.
    @returns: A netloc string suitable for urlunsplit.
    """
    bracketed = f"[{host}]" if ":" in host else host
    return f"{bracketed}:{port}" if port is not None else bracketed


# ── URL validation ───────────────────────────────────────────


def validate_url(url: str, *, resolver: Resolver | None = None) -> ValidatedTarget:
    """Validate a URL against the SSRF policy and return a sanitized target.

    Restricts scheme to http/https, strips userinfo, resolves the host, and
    rejects if ANY resolved address is internal/special.

    TOCTOU note: this resolves and classifies IPs at validation time, but the
    downstream fetcher re-resolves the hostname independently (see the module
    docstring). Callers that fetch across redirect hops must re-validate each
    hop — see `scraper._guard_route`.

    @param url: Candidate URL.
    @param resolver: Host→addresses resolver (injectable for tests).
    @returns: A ValidatedTarget with a sanitized URL and validated addresses.
    @throws UrlRejected: When the URL violates the policy.
    """
    resolve = resolver or _default_resolver

    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port  # may raise ValueError on an out-of-range port
    except ValueError as exc:
        raise UrlRejected(f"malformed-url: {exc}", url=url) from exc

    if scheme not in ALLOWED_SCHEMES:
        raise UrlRejected(f"scheme-not-allowed: {scheme or '(none)'}", url=url)
    if not host:
        raise UrlRejected("missing-host", url=url)
    if port is not None and port == 0:
        # Port 0 parses as valid but is not a connectable port (it means
        # "OS-assigned"); reject it rather than handing the fetcher a dead target.
        raise UrlRejected("invalid-port: 0", url=url)
    if host.lower() in BLOCKED_HOSTNAMES:
        raise UrlRejected(f"blocked-hostname: {host}", url=url, host=host)

    # IP literals skip DNS; hostnames resolve. Validate every resolved address.
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = resolve(host)
    if not addresses:
        raise UrlRejected("host-did-not-resolve", url=url, host=host)

    for ip in addresses:
        reason = classify_ip(ip)
        if reason is not None:
            raise UrlRejected(f"blocked-address: {reason}", url=url, host=host)

    sanitized = urlunsplit(
        (scheme, _format_netloc(host, port), parts.path, parts.query, parts.fragment)
    )
    return ValidatedTarget(
        url=sanitized,
        scheme=scheme,
        host=host,
        port=port,
        addresses=tuple(addresses),
    )


# ── Async validation ─────────────────────────────────────────


async def validate_url_async(url: str, *, resolver: Resolver | None = None) -> ValidatedTarget:
    """Validate a URL without blocking the event loop.

    `validate_url` resolves the host via the system resolver, which is a
    blocking `socket.getaddrinfo` call. In async call sites (the server handler,
    `crawl_url`, `jina_fetch`, the pipeline) that blocks the whole loop. This
    wrapper offloads the same work to a worker thread via `asyncio.to_thread`.

    @param url: Candidate URL.
    @param resolver: Host→addresses resolver (injectable for tests).
    @returns: A ValidatedTarget with a sanitized URL and validated addresses.
    @throws UrlRejected: When the URL violates the policy.
    """
    return await asyncio.to_thread(validate_url, url, resolver=resolver)
