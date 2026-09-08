"""Bounded HTTP retrieval with redirect-by-redirect SSRF controls."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from http.client import HTTPResponse
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener

import httpx

from .security import Resolver, ValidatedURL, system_resolver, validate_https_url

SEC_USER_AGENT = "VD-AI-Digest/1.0 (venture-debt research; mikesupdateagent@agentmail.to)"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class FetchError(RuntimeError):
    """Base exception for bounded fetch failures."""


class FetchLimitError(FetchError):
    """Raised when a response exceeds a configured resource cap."""


class FetchHTTPError(FetchError):
    """Raised for non-success terminal HTTP responses."""

    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"HTTP {status_code} for {url}")
        self.status_code = status_code
        self.url = url


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class _BlockingResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


def _urllib_get(
    url: str,
    headers: Mapping[str, str],
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> _BlockingResponse:
    """One no-redirect stdlib request for public sites that reject httpx's TLS profile."""

    if not url.casefold().startswith("https://"):
        raise FetchError("Compatibility transport requires HTTPS")
    # The async caller validates scheme, domain and resolved IP before every hop.
    request = Request(url, headers=dict(headers), method="GET")  # noqa: S310
    opener = build_opener(_NoRedirectHandler())
    response: HTTPResponse | HTTPError
    try:
        response = opener.open(request, timeout=timeout_seconds)
    except HTTPError as exc:
        response = exc
    except (OSError, URLError) as exc:
        raise FetchError("Compatibility transport request failed") from exc
    try:
        status_code = int(response.status)
        response_headers = {
            str(key).lower(): str(value) for key, value in response.headers.items()
        }
        length_header = response_headers.get("content-length")
        if length_header:
            try:
                announced_length = int(length_header)
            except ValueError:
                announced_length = 0
            if announced_length > max_bytes:
                raise FetchLimitError("Response Content-Length exceeds byte cap")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise FetchLimitError("Response body exceeds byte cap")
        return _BlockingResponse(status_code, response_headers, body)
    finally:
        response.close()


@dataclass(frozen=True, slots=True)
class FetchLimits:
    max_bytes: int = 1_000_000
    timeout_seconds: float = 20.0
    max_redirects: int = 3

    def __post_init__(self) -> None:
        if not 1 <= self.max_bytes <= 10_000_000:
            raise ValueError("max_bytes must be between 1 and 10,000,000")
        if not 0.1 <= self.timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 0.1 and 120")
        if not 0 <= self.max_redirects <= 10:
            raise ValueError("max_redirects must be between 0 and 10")


@dataclass(frozen=True, slots=True)
class RedirectHop:
    from_url: str
    to_url: str
    status_code: int


@dataclass(frozen=True, slots=True)
class FetchResponse:
    requested_url: str
    url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    elapsed_ms: int
    redirect_chain: tuple[RedirectHop, ...] = field(default_factory=tuple)
    resolved_ips: tuple[str, ...] = field(default_factory=tuple)


class SafeFetcher:
    """Fetch HTTPS sources without following an unvalidated redirect."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        resolver: Resolver = system_resolver,
        user_agent: str = SEC_USER_AGENT,
        dns_timeout_seconds: float = 5.0,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._user_agent = user_agent
        self._dns_timeout_seconds = dns_timeout_seconds

    async def validate(
        self,
        url: str,
        allowed_domains: str | Iterable[str],
    ) -> ValidatedURL:
        return await validate_https_url(
            url,
            allowed_domains,
            resolver=self._resolver,
            dns_timeout_seconds=self._dns_timeout_seconds,
        )

    async def fetch(
        self,
        url: str,
        allowed_domains: str | Iterable[str],
        *,
        limits: FetchLimits | None = None,
        headers: Mapping[str, str] | None = None,
        compatibility_transport: bool = False,
    ) -> FetchResponse:
        """Fetch one bounded response, validating DNS before each network hop."""

        active_limits = limits or FetchLimits()
        request_headers = {
            "Accept": "application/json, application/atom+xml;q=0.9, "
            "application/rss+xml;q=0.9, text/html;q=0.8, text/plain;q=0.7",
            "User-Agent": self._user_agent,
        }
        if headers:
            request_headers.update({str(key): str(value) for key, value in headers.items()})

        requested_url = url
        current_url = url
        redirects: list[RedirectHop] = []
        observed_ips: list[str] = []
        started = time.monotonic()

        try:
            async with asyncio.timeout(active_limits.timeout_seconds):
                while True:
                    validated = await self.validate(current_url, allowed_domains)
                    observed_ips.extend(validated.resolved_ips)
                    current_url = validated.url
                    if compatibility_transport:
                        blocking_response = await asyncio.to_thread(
                            _urllib_get,
                            current_url,
                            request_headers,
                            timeout_seconds=active_limits.timeout_seconds,
                            max_bytes=active_limits.max_bytes,
                        )
                        response_status = blocking_response.status_code
                        response_headers = blocking_response.headers
                        response_body = blocking_response.body
                    else:
                        timeout = httpx.Timeout(active_limits.timeout_seconds)
                        async with self._client.stream(
                            "GET",
                            current_url,
                            headers=request_headers,
                            follow_redirects=False,
                            timeout=timeout,
                        ) as response:
                            response_status = response.status_code
                            response_headers = {
                                key.lower(): value for key, value in response.headers.items()
                            }
                            if response_status == 304:
                                response_body = b""
                            elif 200 <= response_status < 300:
                                length_header = response.headers.get("content-length")
                                if length_header:
                                    try:
                                        announced_length = int(length_header)
                                    except ValueError:
                                        announced_length = 0
                                    if announced_length > active_limits.max_bytes:
                                        raise FetchLimitError(
                                            "Response Content-Length exceeds byte cap"
                                        )
                                chunks: list[bytes] = []
                                bytes_read = 0
                                async for chunk in response.aiter_bytes():
                                    bytes_read += len(chunk)
                                    if bytes_read > active_limits.max_bytes:
                                        raise FetchLimitError("Response body exceeds byte cap")
                                    chunks.append(chunk)
                                response_body = b"".join(chunks)
                            else:
                                response_body = b""

                    if response_status in REDIRECT_STATUSES:
                        location = response_headers.get("location")
                        if not location:
                            raise FetchError("Redirect response did not include Location")
                        if len(redirects) >= active_limits.max_redirects:
                            raise FetchLimitError("Redirect limit exceeded")
                        next_url = urljoin(current_url, location)
                        await self.validate(next_url, allowed_domains)
                        redirects.append(
                            RedirectHop(
                                from_url=current_url,
                                to_url=next_url,
                                status_code=response_status,
                            )
                        )
                        current_url = next_url
                        continue

                    if response_status == 304:
                        body = b""
                    elif 200 <= response_status < 300:
                        body = response_body
                    else:
                        raise FetchHTTPError(response_status, current_url)

                    elapsed_ms = round((time.monotonic() - started) * 1000)
                    return FetchResponse(
                        requested_url=requested_url,
                        url=current_url,
                        status_code=response_status,
                        headers=response_headers,
                        body=body,
                        elapsed_ms=elapsed_ms,
                        redirect_chain=tuple(redirects),
                        resolved_ips=tuple(dict.fromkeys(observed_ips)),
                    )
        except TimeoutError as exc:
            raise FetchLimitError("Fetch exceeded total time cap") from exc
