from __future__ import annotations

import asyncio

import httpx
import pytest

from vdai.fetch import (
    SEC_USER_AGENT,
    FetchLimitError,
    FetchLimits,
    SafeFetcher,
    _BlockingResponse,
)
from vdai.security import SecurityPolicyError

S001_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=D&company=&dateb="
    "&owner=include&start=0&count=100&output=atom"
)


async def public_resolver(_hostname: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


async def test_collector_fetch_sends_descriptive_sec_user_agent() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok", headers={"ETag": "revision-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await SafeFetcher(client, resolver=public_resolver).fetch(
            "https://data.sec.gov/submissions/example.json",
            ("sec.gov",),
        )

    assert response.body == b"ok"
    assert captured[0].headers["user-agent"] == SEC_USER_AGENT
    assert "mikesupdateagent@agentmail.to" in SEC_USER_AGENT


async def test_collector_fetch_preserves_real_s001_query_request() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="<feed xmlns='http://www.w3.org/2005/Atom'/>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await SafeFetcher(client, resolver=public_resolver).fetch(
            S001_URL,
            ("www.sec.gov", "data.sec.gov"),
        )

    assert str(captured[0].url) == S001_URL


async def test_collector_fetch_validates_every_redirect() -> None:
    requested: list[str] = []
    resolved: list[str] = []

    async def resolver(hostname: str, _port: int) -> tuple[str, ...]:
        resolved.append(hostname)
        return ("93.184.216.34",)

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200, text="done")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await SafeFetcher(client, resolver=resolver).fetch(
            "https://news.example.com/start",
            ("example.com",),
        )

    assert requested == ["https://news.example.com/start", "https://news.example.com/final"]
    assert len(resolved) >= 3
    assert response.redirect_chain[0].status_code == 302


async def test_collector_fetch_blocks_private_redirect_before_second_request() -> None:
    requested: list[str] = []

    async def resolver(hostname: str, _port: int) -> tuple[str, ...]:
        if hostname == "internal.example.com":
            return ("127.0.0.1",)
        return ("93.184.216.34",)

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://internal.example.com/private"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SecurityPolicyError, match="blocked address"):
            await SafeFetcher(client, resolver=resolver).fetch(
                "https://news.example.com/start",
                ("example.com",),
            )

    assert requested == ["https://news.example.com/start"]


async def test_collector_fetch_enforces_announced_and_streamed_byte_caps() -> None:
    async def announced_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"small", headers={"Content-Length": "500"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(announced_handler)) as client:
        with pytest.raises(FetchLimitError, match="Content-Length"):
            await SafeFetcher(client, resolver=public_resolver).fetch(
                "https://example.com/large",
                ("example.com",),
                limits=FetchLimits(max_bytes=100),
            )

    class ChunkedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 60
            yield b"x" * 60

    async def streamed_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkedStream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(streamed_handler)) as client:
        with pytest.raises(FetchLimitError, match="body exceeds"):
            await SafeFetcher(client, resolver=public_resolver).fetch(
                "https://example.com/large",
                ("example.com",),
                limits=FetchLimits(max_bytes=100),
            )


async def test_collector_fetch_enforces_total_timeout() -> None:
    async def slow_handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, text="late")

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow_handler)) as client:
        with pytest.raises(FetchLimitError, match="time cap"):
            await SafeFetcher(client, resolver=public_resolver).fetch(
                "https://example.com/slow",
                ("example.com",),
                limits=FetchLimits(timeout_seconds=0.1),
            )


async def test_collector_fetch_compatibility_transport_preserves_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    def fake_urllib_get(url: str, _headers, **_kwargs) -> _BlockingResponse:
        requested.append(url)
        return _BlockingResponse(
            status_code=200,
            headers={"content-type": "text/html"},
            body=b"<main>public news</main>",
        )

    monkeypatch.setattr("vdai.fetch._urllib_get", fake_urllib_get)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: None)) as client:
        response = await SafeFetcher(client, resolver=public_resolver).fetch(
            "https://example.com/news",
            ("example.com",),
            compatibility_transport=True,
        )

    assert requested == ["https://example.com/news"]
    assert response.body == b"<main>public news</main>"
