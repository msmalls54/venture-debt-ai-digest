"""Network and URL safety controls for untrusted source configuration."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit


class SecurityPolicyError(ValueError):
    """Raised when a URL or resolved address violates the collector policy."""


Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


@dataclass(frozen=True, slots=True)
class ValidatedURL:
    """A normalized HTTPS URL and the public addresses observed during validation."""

    url: str
    hostname: str
    resolved_ips: tuple[str, ...]


def normalize_allowed_domains(domains: str | Iterable[str]) -> tuple[str, ...]:
    """Normalize a source allowlist without accepting URL paths or wildcard ambiguity."""

    values = domains.replace(";", ",").split(",") if isinstance(domains, str) else domains
    normalized: list[str] = []
    for value in values:
        domain = str(value).strip().lower().rstrip(".")
        if not domain:
            continue
        if "://" in domain or "/" in domain or "@" in domain or "*" in domain:
            raise SecurityPolicyError(f"Invalid allowlisted domain: {value!r}")
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise SecurityPolicyError(f"Invalid allowlisted domain: {value!r}") from exc
        if domain not in normalized:
            normalized.append(domain)
    if not normalized:
        raise SecurityPolicyError("At least one allowed domain is required")
    return tuple(normalized)


def hostname_is_allowed(hostname: str, allowed_domains: Iterable[str]) -> bool:
    """Allow an exact host or a real subdomain, never a suffix lookalike."""

    host = hostname.lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)


def address_is_public(address: str) -> bool:
    """Return True only for globally routable unicast addresses."""

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_reserved
        or parsed.is_multicast
        or parsed.is_unspecified
    )


async def system_resolver(hostname: str, port: int) -> Sequence[str]:
    """Resolve a hostname asynchronously and return every address family result."""

    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(
        hostname,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    return tuple(dict.fromkeys(record[4][0] for record in records))


def _normalize_https_parts(url: str) -> tuple[SplitResult, str]:
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise SecurityPolicyError("URL contains control characters")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "https":
        raise SecurityPolicyError("Only HTTPS source URLs are allowed")
    if not parts.hostname:
        raise SecurityPolicyError("URL is missing a hostname")
    if parts.username is not None or parts.password is not None:
        raise SecurityPolicyError("Credentials in source URLs are forbidden")
    try:
        hostname = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parts.port
    except (UnicodeError, ValueError) as exc:
        raise SecurityPolicyError("URL has an invalid hostname or port") from exc
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise SecurityPolicyError("Local hostnames are forbidden")
    if port not in (None, 443):
        raise SecurityPolicyError("Only the standard HTTPS port is allowed")
    return parts, hostname


def _normalized_url(parts: SplitResult, hostname: str) -> str:
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    path = parts.path or "/"
    return urlunsplit(("https", host, path, parts.query, ""))


async def validate_https_url(
    url: str,
    allowed_domains: str | Iterable[str],
    *,
    resolver: Resolver = system_resolver,
    dns_timeout_seconds: float = 5.0,
) -> ValidatedURL:
    """Validate scheme, allowlist, DNS answers, and normalized URL form.

    Callers must invoke this before the initial request and again for every redirect.
    """

    domains = normalize_allowed_domains(allowed_domains)
    parts, hostname = _normalize_https_parts(url)
    if not hostname_is_allowed(hostname, domains):
        raise SecurityPolicyError(f"Hostname {hostname!r} is outside the source allowlist")

    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        try:
            addresses = await asyncio.wait_for(
                resolver(hostname, 443),
                timeout=dns_timeout_seconds,
            )
        except TimeoutError as exc:
            raise SecurityPolicyError("DNS resolution exceeded its time limit") from exc
        except OSError as exc:
            raise SecurityPolicyError("DNS resolution failed") from exc
    else:
        addresses = (str(literal),)

    unique_addresses = tuple(dict.fromkeys(str(address) for address in addresses))
    if not unique_addresses:
        raise SecurityPolicyError("DNS resolution returned no addresses")
    blocked = [address for address in unique_addresses if not address_is_public(address)]
    if blocked:
        raise SecurityPolicyError(f"DNS resolution returned blocked address space: {blocked[0]}")

    return ValidatedURL(
        url=_normalized_url(parts, hostname),
        hostname=hostname,
        resolved_ips=unique_addresses,
    )
