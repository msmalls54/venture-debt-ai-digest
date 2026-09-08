"""Async, evidence-neutral source collection into strict CandidateV1 records."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from .fetch import FetchError, FetchLimits, FetchResponse, SafeFetcher
from .models import CandidateV1, ParserLane, SourceJob
from .parsers import (
    ParsedItem,
    candidate_key,
    parse_html,
    parse_html_document,
    parse_lane,
    parse_sitemap,
    revision_hash,
)
from .security import SecurityPolicyError

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,application/rss+xml;q=0.8,*/*;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.8",
}
SVB_NEWS_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://www.svb.com/newsroom/",
    "X-Requested-With": "XMLHttpRequest",
}


class CollectionError(RuntimeError):
    """A source job could not produce a safe deterministic collection result."""


@dataclass(frozen=True, slots=True)
class JobCollectionResult:
    """Per-job isolation keeps one broken site from cancelling healthy sources."""

    source_job_key: str
    candidates: tuple[CandidateV1, ...] = ()
    error_type: str | None = None
    error_detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.error_type is None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Collector:
    """Collect source jobs with bounded job concurrency and deterministic ordering."""

    def __init__(
        self,
        fetcher: SafeFetcher,
        *,
        max_concurrency: int = 4,
        request_timeout_seconds: float = 20.0,
        max_redirects: int = 3,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not 1 <= max_concurrency <= 16:
            raise ValueError("max_concurrency must be between 1 and 16")
        if not 0.1 <= request_timeout_seconds <= 120:
            raise ValueError("request_timeout_seconds must be between 0.1 and 120")
        if not 0 <= max_redirects <= 10:
            raise ValueError("max_redirects must be between 0 and 10")
        self._fetcher = fetcher
        self._max_concurrency = max_concurrency
        self._request_timeout_seconds = request_timeout_seconds
        self._max_redirects = max_redirects
        self._clock = clock

    async def collect(
        self,
        jobs: Iterable[SourceJob],
        *,
        run_id: str,
    ) -> tuple[JobCollectionResult, ...]:
        """Collect every job without allowing one failure to cancel its peers."""

        if not run_id.strip():
            raise ValueError("run_id is required")
        job_list = list(jobs)
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def run_one(index: int, job: SourceJob) -> tuple[int, JobCollectionResult]:
            async with semaphore:
                try:
                    candidates = await self.collect_job(job, run_id=run_id)
                except Exception as exc:
                    result = JobCollectionResult(
                        source_job_key=job.source_job_key,
                        error_type=type(exc).__name__,
                        error_detail=str(exc)[:1_000],
                    )
                else:
                    result = JobCollectionResult(
                        source_job_key=job.source_job_key,
                        candidates=candidates,
                    )
                return index, result

        indexed = await asyncio.gather(*(run_one(index, job) for index, job in enumerate(job_list)))
        indexed.sort(key=lambda item: item[0])
        return tuple(result for _, result in indexed)

    async def collect_job(
        self,
        job: SourceJob,
        *,
        run_id: str,
    ) -> tuple[CandidateV1, ...]:
        """Collect one ready job; no model or page-supplied instruction is executed."""

        if not job.ready:
            errors = ", ".join(job.configuration_errors) or "unresolved source URL"
            raise CollectionError(f"Source job is not ready: {errors}")
        source = job.source
        limits = FetchLimits(
            max_bytes=source.max_bytes,
            timeout_seconds=self._request_timeout_seconds,
            max_redirects=self._max_redirects,
        )
        response = await self._fetcher.fetch(
            job.url,
            source.allowed_domains,
            limits=limits,
            headers=_headers_for_profile(source.headers_profile),
            compatibility_transport=_uses_compatibility_transport(source.headers_profile),
        )
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None:
            raise CollectionError("Collector clock must return a timezone-aware datetime")
        retrieved_at = retrieved_at.astimezone(UTC)

        if job.parser_lane is ParserLane.SITEMAP_HTML:
            item_responses = await self._collect_sitemap_pages(job, response, limits)
        else:
            parsed = parse_lane(
                job.parser_lane.value,
                response.body,
                base_url=response.url,
                max_items=source.max_items,
            )
            if job.parser_lane is ParserLane.HTML and "detail" in source.parser.casefold():
                item_responses = await self._hydrate_html_details(job, parsed, response, limits)
            else:
                item_responses = [(item, response) for item in parsed]

        candidates: list[CandidateV1] = []
        observed_ids: set[str] = set()
        for item, item_response in item_responses[: source.max_items]:
            candidate = await self._candidate_from_item(
                job,
                item,
                item_response,
                run_id=run_id,
                retrieved_at=retrieved_at,
            )
            if candidate.candidate_id in observed_ids:
                continue
            observed_ids.add(candidate.candidate_id)
            candidates.append(candidate)
        return tuple(candidates)

    async def _hydrate_html_details(
        self,
        job: SourceJob,
        items: Sequence[ParsedItem],
        listing_response: FetchResponse,
        limits: FetchLimits,
    ) -> list[tuple[ParsedItem, FetchResponse]]:
        """Boundedly enrich undated listing cards from their own detail pages."""

        source = job.source
        output: list[tuple[ParsedItem, FetchResponse]] = []
        remaining_detail_pages = source.max_pages
        for item in items:
            if (
                item.published_at is not None
                or item.url == listing_response.url
                or remaining_detail_pages <= 0
            ):
                output.append((item, listing_response))
                continue
            remaining_detail_pages -= 1
            try:
                detail_response = await self._fetcher.fetch(
                    item.url,
                    source.allowed_domains,
                    limits=limits,
                    headers=_headers_for_profile(source.headers_profile),
                    compatibility_transport=_uses_compatibility_transport(source.headers_profile),
                )
                detail = parse_html_document(detail_response.body, base_url=detail_response.url)
            except (FetchError, SecurityPolicyError):
                detail = None
            if detail is None:
                output.append((item, listing_response))
                continue
            hydrated = ParsedItem(
                title=item.title,
                url=item.url,
                item_id=item.item_id,
                body_text=(
                    detail.body_text
                    if len(detail.body_text) > len(item.body_text)
                    else item.body_text
                ),
                summary_text=(detail.summary_text or item.summary_text),
                published_at=detail.published_at,
                native_ids=item.native_ids,
                parse_warnings=tuple(sorted(set(item.parse_warnings + detail.parse_warnings))),
            )
            output.append((hydrated, detail_response))
        return output

    async def _collect_sitemap_pages(
        self,
        job: SourceJob,
        sitemap_response: FetchResponse,
        limits: FetchLimits,
    ) -> list[tuple[ParsedItem, FetchResponse]]:
        source = job.source
        entries = parse_sitemap(
            sitemap_response.body,
            base_url=sitemap_response.url,
            max_items=min(source.max_items, source.max_pages),
        )
        output: list[tuple[ParsedItem, FetchResponse]] = []
        queue = list(entries)
        pages_fetched = 0
        while queue and pages_fetched < source.max_pages:
            if len(output) >= source.max_items:
                break
            entry = queue.pop(0)
            page_response = await self._fetcher.fetch(
                entry.url,
                source.allowed_domains,
                limits=limits,
                headers=_headers_for_profile(source.headers_profile),
            )
            pages_fetched += 1
            if entry.is_sitemap:
                remaining_pages = source.max_pages - pages_fetched
                if remaining_pages > 0:
                    nested = parse_sitemap(
                        page_response.body,
                        base_url=page_response.url,
                        max_items=min(source.max_items, remaining_pages),
                    )
                    queue.extend(nested)
                continue
            remaining = source.max_items - len(output)
            page_items = parse_html(
                page_response.body,
                base_url=page_response.url,
                max_items=remaining,
            )
            output.extend((item, page_response) for item in page_items[:remaining])
        return output

    async def _candidate_from_item(
        self,
        job: SourceJob,
        item: ParsedItem,
        response: FetchResponse,
        *,
        run_id: str,
        retrieved_at: datetime,
    ) -> CandidateV1:
        source = job.source
        warnings = list(item.parse_warnings)
        raw_url = item.url
        try:
            validated_item = await self._fetcher.validate(item.url, source.allowed_domains)
            canonical_url = validated_item.url
        except SecurityPolicyError:
            validated_source = await self._fetcher.validate(response.url, source.allowed_domains)
            canonical_url = validated_source.url
            raw_url = canonical_url
            warnings.append("CANONICAL_URL_ALLOWLIST_FALLBACK")

        title = " ".join(item.title.split()).strip()
        body = "\n".join(line.strip() for line in item.body_text.splitlines() if line.strip())
        if not title:
            raise CollectionError("Parsed candidate has no title")
        if not body:
            raise CollectionError("Parsed candidate has no body text")
        body = body[:15_000]
        summary = " ".join(item.summary_text.split())[:5_000]
        item_id = item.item_id.strip() or canonical_url
        content_hash = revision_hash(title, body)
        deterministic_id = candidate_key(source.source_id, item_id, content_hash)
        native_ids = {
            str(key): str(value)
            for key, value in item.native_ids.items()
            if value not in (None, "")
        }
        redirect_chain = [
            {
                "from_url": hop.from_url,
                "to_url": hop.to_url,
                "status_code": hop.status_code,
            }
            for hop in response.redirect_chain
        ]

        return CandidateV1.model_validate(
            {
                "schema_version": "candidate.v1",
                "run_id": run_id,
                "candidate_id": deterministic_id,
                "source_id": source.source_id,
                "source_job_key": job.source_job_key,
                "source_name": source.source_name,
                "section_hint": source.section,
                "source_class": source.source_tier,
                "parser_lane": job.parser_lane,
                "priority": source.priority,
                "primary_source": source.primary_source,
                "discovery_only": source.discovery_only,
                "retrieved_at": retrieved_at,
                "item_id": item_id,
                "raw_url": raw_url,
                "canonical_url": canonical_url,
                "title": title,
                "published_at": item.published_at,
                "summary_text": summary,
                "body_text": body,
                "watchlist_hits": (),
                "content_sha256": content_hash,
                "dedupe_key": deterministic_id,
                "native_ids": native_ids,
                "fetch_meta": {
                    "http_status": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "bytes": len(response.body),
                    "etag": response.headers.get("etag", ""),
                    "last_modified": response.headers.get("last-modified", ""),
                    "elapsed_ms": response.elapsed_ms,
                    "redirect_chain": redirect_chain,
                    "resolved_ips": list(response.resolved_ips),
                },
                "parse_meta": {
                    "parser_version": source.parser_version,
                    "warnings": sorted(set(warnings)),
                    "untrusted_page_instructions_executed": False,
                },
                "jurisdictions": source.region,
            }
        )


def flatten_candidates(results: Sequence[JobCollectionResult]) -> tuple[CandidateV1, ...]:
    """Flatten successful outcomes without hiding per-job errors from the caller."""

    return tuple(candidate for result in results for candidate in result.candidates)


def _headers_for_profile(profile: str) -> dict[str, str] | None:
    """Map Sheet labels to fixed headers; source rows never supply arbitrary header values."""

    normalized = profile.strip().casefold()
    if normalized == "svb":
        return dict(SVB_NEWS_HEADERS)
    if normalized in {"browser", "compat"}:
        return dict(BROWSER_HEADERS)
    return None


def _uses_compatibility_transport(profile: str) -> bool:
    return profile.strip().casefold() == "compat"
