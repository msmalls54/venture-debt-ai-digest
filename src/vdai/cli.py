"""Safe command-line entry point for the single Railway worker."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .agentmail import AgentMailClient
from .collector import Collector
from .config import AppSettings, ConfigurationError, expand_source_jobs
from .editor import DailyEditor
from .evidence import EvidenceAnalyst
from .fetch import SafeFetcher
from .openrouter import OPENROUTER_MODEL, OpenRouterClient
from .orchestrator import DigestOrchestrator, _candidate_is_relevant
from .renderer import RenderedEmail, render_digest
from .research import WebResearchScout
from .sheets import GoogleSheetsRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vdai-digest",
        description="Evidence-first venture debt and AI digest worker",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate secrets and Sheet access")
    validate.add_argument(
        "--offline",
        action="store_true",
        help="Validate environment structure without reading the Sheet",
    )

    run = subparsers.add_parser("run", help="Collect due sources and run the daily send gate")
    _add_run_arguments(run)

    send_once = subparsers.add_parser(
        "send-once",
        help="Run one idempotent live delivery while preserving every non-clock gate",
    )
    _add_run_arguments(send_once)
    send_once.add_argument(
        "--revision",
        help=(
            "Explicit idempotent revision label for a same-day full update; the same "
            "label can never send twice"
        ),
    )
    send_once.set_defaults(send_once=True)

    collect = subparsers.add_parser(
        "collect", help="Collect due sources without editing or sending"
    )
    _add_run_arguments(collect, collect_only=True)

    fetch_canary = subparsers.add_parser(
        "fetch-canary",
        help="Fetch configured sources without models, Sheet writes, or email",
    )
    fetch_canary.add_argument(
        "--source-id",
        action="append",
        dest="source_ids",
        help="Limit the canary to a configured source ID; may be repeated",
    )

    canary = subparsers.add_parser(
        "render-canary", help="Render a network-free black-and-silver sample"
    )
    canary.add_argument("--output-dir", type=Path, default=Path("artifacts/canary"))
    return parser


def _add_run_arguments(parser: argparse.ArgumentParser, *, collect_only: bool = False) -> None:
    parser.set_defaults(collect_only=collect_only, send_once=False)
    parser.add_argument("--dry-run", action="store_true", help="Do not write or send")
    parser.add_argument(
        "--force-editor",
        action="store_true",
        help="Build a preview outside the normal send window",
    )
    parser.add_argument(
        "--allow-degraded-preview",
        action="store_true",
        help="Allow only a dry-run preview when coverage is degraded",
    )
    parser.add_argument(
        "--full-brief",
        action="store_true",
        help=(
            "Use the eight-day catch-up window while retaining published-story "
            "deduplication; intended for an explicit full preview or send-once"
        ),
    )
    parser.add_argument("--source-id", help="Run one configured source manually")
    parser.add_argument(
        "--preview-dir",
        type=Path,
        help="Write the rendered HTML and plain text locally when an edition is built",
    )


def _settings() -> AppSettings:
    settings = AppSettings()
    if settings.openrouter_model != OPENROUTER_MODEL:
        raise ConfigurationError(
            f"OPENROUTER_MODEL must remain pinned to {OPENROUTER_MODEL} for launch"
        )
    if settings.openrouter_reasoning_effort.strip().lower() != "high":
        raise ConfigurationError("OPENROUTER_REASONING_EFFORT must remain high for launch")
    return settings


def _validate_command(*, offline: bool) -> dict[str, Any]:
    settings = _settings()
    settings.service_account_info()
    result: dict[str, Any] = {
        "status": "VALID",
        "google_credential": "CONFIGURED",
        "openrouter_key_configured": bool(settings.openrouter_api_key.get_secret_value()),
        "agentmail_key_configured": bool(settings.agentmail_api_key.get_secret_value()),
        "test_mode": settings.test_mode,
        "send_enabled": settings.agentmail_send_enabled,
        "model": settings.openrouter_model,
    }
    if offline:
        return result

    repository = GoogleSheetsRepository.from_settings(settings)
    sources = repository.load_sources(active_only=True)
    sheet_settings = repository.load_settings()
    recipients = repository.load_recipients(
        exclude_addresses=(settings.agentmail_inbox,), max_recipients=49
    )
    result.update(
        {
            "sheet_access": "READABLE",
            "active_source_count": len(sources),
            "company_watch_count": len(repository.load_company_watchlist()),
            "deposit_watch_count": len(repository.load_deposit_watch()),
            "recipient_count": len(recipients.recipients),
            "invalid_recipient_count": recipients.invalid_count,
            "sheet_test_mode": bool(sheet_settings.get("test_mode", True)),
            "sheet_send_enabled": bool(sheet_settings.get("agentmail_send_enabled", False)),
        }
    )
    return result


async def _run_command(args: argparse.Namespace) -> tuple[dict[str, Any], RenderedEmail | None]:
    settings = _settings()
    if not settings.worker_enabled:
        return (
            {
                "status": "PAUSED",
                "delivery_status": "PAUSED",
                "reason_code": "WORKER_DISABLED",
            },
            None,
        )
    if _is_scheduled_run(args) and not _inside_collection_hour(settings):
        return (
            {
                "status": "PAUSED",
                "delivery_status": "NOT_LOCAL_COLLECTION_HOUR",
                "reason_code": "DST_GUARD",
            },
            None,
        )
    repository = GoogleSheetsRepository.from_settings(settings)

    async with AsyncExitStack() as stack:
        source_http = await stack.enter_async_context(
            httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(settings.source_timeout_seconds),
            )
        )
        openrouter = await stack.enter_async_context(
            OpenRouterClient(settings.openrouter_api_key.get_secret_value())
        )
        agentmail = await stack.enter_async_context(
            AgentMailClient(
                api_key=settings.agentmail_api_key.get_secret_value(),
                inbox=settings.agentmail_inbox,
                send_enabled=settings.agentmail_send_enabled,
                test_mode=settings.test_mode,
            )
        )
        collector = Collector(
            SafeFetcher(source_http),
            max_concurrency=settings.collection_concurrency,
            request_timeout_seconds=settings.source_timeout_seconds,
        )
        orchestrator = DigestOrchestrator(
            settings=settings,
            repository=repository,
            collector=collector,
            evidence=EvidenceAnalyst(openrouter),
            editor=DailyEditor(openrouter),
            agentmail=agentmail,
            researcher=WebResearchScout(openrouter),
        )
        summary = await orchestrator.run_once(
            dry_run=bool(args.dry_run),
            collect_only=bool(args.collect_only),
            force_editor=bool(args.force_editor),
            send_once=bool(args.send_once),
            require_story=bool(args.send_once),
            allow_degraded_preview=bool(args.allow_degraded_preview),
            full_brief=bool(args.full_brief),
            send_revision=getattr(args, "revision", None),
            manual_source_id=args.source_id,
        )
        return summary.as_dict(), orchestrator.last_rendered


async def _fetch_canary_command(args: argparse.Namespace) -> dict[str, Any]:
    """Exercise only the source-fetch boundary for the live source registry."""

    settings = _settings()
    repository = GoogleSheetsRepository.from_settings(settings)
    configured_sources = repository.load_sources(active_only=False)
    selected = _select_canary_sources(configured_sources, args.source_ids)
    # A staged source may still be inactive in the Sheet. The read-only canary
    # must exercise it before activation instead of producing a false-green zero-job run.
    jobs = expand_source_jobs(selected, repository.load_company_watchlist(), manual=True)
    if len(jobs) < len(selected):
        raise ConfigurationError("fetch canary did not expand every requested source")
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(settings.source_timeout_seconds),
    ) as source_http:
        collector = Collector(
            SafeFetcher(source_http),
            max_concurrency=settings.collection_concurrency,
            request_timeout_seconds=settings.source_timeout_seconds,
        )
        results = await collector.collect(
            jobs,
            run_id=f"DRY-FETCH-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
        )
    receipts = []
    failures = 0
    for result in results:
        relevant_candidates = tuple(
            candidate for candidate in result.candidates if _candidate_is_relevant(candidate)
        )
        published = sorted(
            candidate.published_at
            for candidate in relevant_candidates
            if candidate.published_at is not None
        )
        distinct_urls = len({str(candidate.canonical_url) for candidate in relevant_candidates})
        source_url = next(
            (str(job.source.url) for job in jobs if job.source_job_key == result.source_job_key),
            "",
        )
        editorial_error = (
            _canary_editorial_error(relevant_candidates, source_url=source_url)
            if result.succeeded
            else ""
        )
        succeeded = result.succeeded and not editorial_error
        failures += not succeeded
        receipts.append(
            {
                "source_job_key": result.source_job_key,
                "status": "SUCCESS" if succeeded else "FAILED",
                "candidate_count": len(result.candidates),
                "relevant_candidate_count": len(relevant_candidates),
                "dated_candidate_count": len(published),
                "distinct_url_count": distinct_urls,
                "newest_published_at": published[-1].isoformat() if published else "",
                "error_code": str(result.error_type or editorial_error),
                "error_detail": str(
                    result.error_detail
                    or (
                        "source fetched, but did not produce a usable dated story stream"
                        if editorial_error
                        else ""
                    )
                )[:300],
                "raw_candidate_samples": [
                    {
                        "title": candidate.title[:180],
                        "published_at": (
                            candidate.published_at.isoformat()
                            if candidate.published_at is not None
                            else ""
                        ),
                    }
                    for candidate in result.candidates[:3]
                ],
                "candidate_samples": [
                    {
                        "title": candidate.title[:180],
                        "url": str(candidate.canonical_url),
                        "published_at": (
                            candidate.published_at.isoformat()
                            if candidate.published_at is not None
                            else ""
                        ),
                    }
                    for candidate in relevant_candidates[:3]
                ],
            }
        )
    return {
        "status": "FETCH_CANARY_FAILED" if failures else "FETCH_CANARY_PASSED",
        "source_count": len(selected),
        "job_count": len(jobs),
        "successful_job_count": len(jobs) - failures,
        "failed_job_count": failures,
        "candidate_count": sum(len(result.candidates) for result in results),
        "receipts": receipts,
    }


def _canary_editorial_error(candidates: Any, *, source_url: str = "") -> str:
    """Reject transport-green wrappers that cannot support a cited digest story."""

    items = tuple(candidates)
    if not items:
        return "EMPTY_SOURCE"
    if not any(candidate.published_at is not None for candidate in items):
        return "UNDATED_SOURCE"
    distinct_urls = {str(candidate.canonical_url) for candidate in items}
    lane = str(getattr(items[0].parser_lane, "value", items[0].parser_lane)).casefold()
    if lane not in {"api", "json"} and len(items) == 1 and source_url:
        normalized_source = source_url.rstrip("/").casefold()
        normalized_candidate = str(items[0].canonical_url).rstrip("/").casefold()
        if normalized_candidate == normalized_source:
            return "COLLAPSED_SOURCE"
    if lane not in {"api", "json"} and len(items) >= 2 and len(distinct_urls) < 2:
        return "COLLAPSED_SOURCE"
    return ""


def _select_canary_sources(
    configured_sources: list[Any], requested_ids: list[str] | None
) -> list[Any]:
    """Select active registry rows by default and allow explicit inactive-source checks."""

    sources_by_id = {source.source_id: source for source in configured_sources}
    requested = tuple(
        dict.fromkeys(
            requested_ids or [source.source_id for source in configured_sources if source.active]
        )
    )
    if not requested:
        raise ConfigurationError("source registry has no active sources to audit")
    missing = [source_id for source_id in requested if source_id not in sources_by_id]
    if missing:
        raise ConfigurationError(f"source is missing from the registry: {missing[0]}")
    return [sources_by_id[source_id].model_copy(update={"active": True}) for source_id in requested]


def _is_scheduled_run(args: argparse.Namespace) -> bool:
    return (
        args.command == "run"
        and not bool(args.dry_run)
        and not bool(args.force_editor)
        and not bool(args.source_id)
    )


def _inside_collection_hour(settings: AppSettings, now: datetime | None = None) -> bool:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("collection clock must be timezone-aware")
    return value.astimezone(ZoneInfo(settings.timezone)).hour == settings.send_hour_local


def _write_preview(rendered: RenderedEmail, output_dir: Path) -> dict[str, str]:
    target = output_dir.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    html_path = target / "digest-preview.html"
    text_path = target / "digest-preview.txt"
    dashboard_path = target / "digest-dashboard.html"
    html_path.write_text(rendered.html, encoding="utf-8")
    text_path.write_text(rendered.text, encoding="utf-8")
    result = {"preview_html": str(html_path), "preview_text": str(text_path)}
    if rendered.browser_html:
        dashboard_path.write_text(rendered.browser_html, encoding="utf-8")
        result["preview_dashboard"] = str(dashboard_path)
    return result


def _canary() -> dict[str, Any]:
    return {
        "edition_date": "2026-08-25",
        "edition": {
            "kicker": "VENTURE DEBT + AI",
            "headline": "The Daily Signal",
            "deck": "Verified capital moves, competitive shifts, and AI releases worth your time.",
        },
        "validated_stories": [
            {
                "section": "lead",
                "headline": "Northstar — $75M credit facility",
                "dek": "The company disclosed a new senior secured facility led by Example Bank.",
                "why_it_matters": (
                    "Fresh debt capacity buys runway without resetting the equity price."
                ),
                "citations": [
                    {
                        "source_id": "CANARY-1",
                        "publisher": "Official filing",
                        "url": "https://example.com/filing",
                    }
                ],
            },
            {
                "section": "competitive_field",
                "headline": "Example Bank — fintech acquisition closes",
                "dek": "The bank completed its acquisition of a treasury software provider.",
                "why_it_matters": ("The deposit fight is moving further into the operating stack."),
                "citations": [
                    {
                        "source_id": "CANARY-2",
                        "publisher": "Company release",
                        "url": "https://example.com/release",
                    }
                ],
            },
            {
                "section": "ai_radar",
                "headline": "Example AI — lower-cost reasoning model",
                "dek": "The vendor released a faster model with structured-output support.",
                "why_it_matters": (
                    "Useful automation gets cheaper, but reliability still needs testing."
                ),
                "citations": [
                    {
                        "source_id": "CANARY-3",
                        "publisher": "Vendor release",
                        "url": "https://example.com/model",
                    }
                ],
            },
        ],
    }


def _exit_code(result: dict[str, Any]) -> int:
    if result.get("status") == "FETCH_CANARY_FAILED":
        return 2
    delivery_status = str(result.get("delivery_status", ""))
    if delivery_status.startswith("FAILED") or delivery_status == "HELD_SOURCE_HEALTH":
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            result = _validate_command(offline=args.offline)
        elif args.command == "fetch-canary":
            result = asyncio.run(_fetch_canary_command(args))
        elif args.command == "render-canary":
            rendered = render_digest(_canary(), test_mode=True)
            result = {"status": "CANARY_RENDERED", **_write_preview(rendered, args.output_dir)}
        else:
            result, rendered = asyncio.run(_run_command(args))
            if rendered is not None and args.preview_dir is not None:
                result.update(_write_preview(rendered, args.preview_dir))
        print(json.dumps(result, sort_keys=True))
        return _exit_code(result)
    except Exception as error:  # one sanitized process boundary for Railway receipts
        print(
            json.dumps(
                {
                    "status": "FAILED_CLOSED",
                    "error_code": type(error).__name__.upper(),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover - console script is the tested boundary
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
