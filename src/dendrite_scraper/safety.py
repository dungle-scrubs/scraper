"""URL safety guard — the single SSRF trust boundary for dendrite-scraper.

Every URL that reaches a fetch path (crawl4ai, Jina, the CLI) must pass through
`validate_url` before any network egress, and every redirect hop must pass
through `revalidate_redirect`. This module owns:

  - scheme restriction (http/https only),
  - credential stripping (no `user:pass@` ever leaves the process),
  - host resolution + IP classification (block loopback, private, link-local,
    reserved, multicast, unspecified, CGNAT/shared, and cloud-metadata targets),
  - per-redirect re-validation with a bounded hop count,
  - the IP-pinning request rewrite used when a target is fetched directly.

It owns the *decision* to allow or block a target. It does NOT perform the
fetch itself — callers do, using the `ValidatedTarget` it returns. Resolution is
injectable (`resolver`) so the policy is unit-testable without DNS or network.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

# ── Constants ────────────────────────────────────────────────

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_MAX_REDIRECTS = 5

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


def classify_ip(ip: str) -> str | None:
    """Return a block reason for an internal/special address, or None if public.

    @param ip: IP address literal (v4 or v6).
    @returns: Reason string when the address must be blocked, else None.
    """
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(ip)
    except ValueError:
        return "unparseable-ip"

    # Unwrap IPv4-mapped/compat IPv6 so ::ffff:127.0.0.1 can't slip past v4 checks.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped

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


def revalidate_redirect(
    location: str,
    *,
    hop: int,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    resolver: Resolver | None = None,
) -> ValidatedTarget:
    """Re-validate a redirect target, enforcing a bounded hop count.

    @param location: Absolute redirect target URL.
    @param hop: 1-based index of this redirect hop.
    @param max_redirects: Maximum allowed hops before rejection.
    @param resolver: Host→addresses resolver (injectable for tests).
    @returns: A ValidatedTarget for the redirect destination.
    @throws UrlRejected: When the hop count is exceeded or the target is blocked.
    """
    if hop > max_redirects:
        raise UrlRejected(f"too-many-redirects: >{max_redirects}", url=location)
    return validate_url(location, resolver=resolver)


# ── IP pinning ───────────────────────────────────────────────


def pin_request_kwargs(target: ValidatedTarget) -> dict[str, object]:
    """Build httpx request kwargs that pin the connection to a validated IP.

    Connects to the already-validated address while preserving the original
    host for the Host header and TLS SNI (httpcore `sni_hostname` extension),
    so the address that was checked is the address that is dialed — closing the
    DNS-rebinding window for direct target fetches.

    @param target: A validated target with at least one resolved address.
    @returns: kwargs (url, headers, extensions) for an httpx request.
    """
    parts = urlsplit(target.url)
    ip = target.addresses[0]
    pinned_netloc = _format_netloc(ip, target.port)
    pinned_url = urlunsplit((parts.scheme, pinned_netloc, parts.path, parts.query, parts.fragment))
    return {
        "url": pinned_url,
        "headers": {"Host": _format_netloc(target.host, target.port)},
        "extensions": {"sni_hostname": target.host},
    }
