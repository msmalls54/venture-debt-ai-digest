from __future__ import annotations

import pytest

from vdai.security import (
    SecurityPolicyError,
    address_is_public,
    hostname_is_allowed,
    normalize_allowed_domains,
    validate_https_url,
)


async def public_resolver(_hostname: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "192.168.1.2",
        "172.16.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "224.0.0.1",
        "0.0.0.0",  # noqa: S104 - test fixture proving unspecified addresses are blocked
    ],
)
def test_collector_security_blocks_non_public_addresses(address: str) -> None:
    assert address_is_public(address) is False


def test_collector_security_normalizes_real_domain_boundaries() -> None:
    domains = normalize_allowed_domains("Example.COM, news.example.com")
    assert domains == ("example.com", "news.example.com")
    assert hostname_is_allowed("www.example.com", domains)
    assert not hostname_is_allowed("example.com.attacker.test", domains)
    assert not hostname_is_allowed("fakeexample.com", domains)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/news",
        "https://user:pass@example.com/news",
        "https://example.com:444/news",
        "https://localhost/news",
        "https://example.com.attacker.test/news",
    ],
)
async def test_collector_security_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(SecurityPolicyError):
        await validate_https_url(url, ("example.com",), resolver=public_resolver)


async def test_collector_security_rejects_private_dns_answer() -> None:
    async def private_resolver(_hostname: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34", "169.254.169.254")

    with pytest.raises(SecurityPolicyError, match="blocked address"):
        await validate_https_url(
            "https://news.example.com/item",
            ("example.com",),
            resolver=private_resolver,
        )


async def test_collector_security_returns_normalized_public_url() -> None:
    validated = await validate_https_url(
        "https://NEWS.Example.com/story#fragment",
        ("example.com",),
        resolver=public_resolver,
    )
    assert validated.url == "https://news.example.com/story"
    assert validated.resolved_ips == ("93.184.216.34",)
