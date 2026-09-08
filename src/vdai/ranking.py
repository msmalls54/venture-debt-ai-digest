"""Stable, evidence-preserving candidate ranking before editorial selection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from dateutil import parser as date_parser


def rank_eligible_events(
    events: Sequence[Mapping[str, Any]],
    published_history: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Suppress exact prior stories, then rank by verified score and recency."""

    if now.tzinfo is None:
        raise ValueError("ranking requires a timezone-aware clock")
    if not 1 <= limit <= 100:
        raise ValueError("ranking limit must be between 1 and 100")
    now = now.astimezone(UTC)

    published_ids: set[str] = set()
    published_hashes: set[str] = set()
    published_urls: set[str] = set()
    for row in published_history:
        for key in ("raw_event_id", "event_id", "candidate_id"):
            value = str(row.get(key, "")).strip()
            if value:
                published_ids.add(value)
        hashes = row.get("content_hashes", row.get("content_hash", ()))
        if isinstance(hashes, str):
            hashes = [hashes]
        if isinstance(hashes, Sequence):
            published_hashes.update(str(value).strip() for value in hashes if str(value).strip())
        canonical_url = str(row.get("canonical_url", "")).strip()
        if canonical_url:
            published_urls.add(canonical_url)

    unique: dict[str, dict[str, Any]] = {}
    for source in events:
        row = dict(source)
        event_id = str(row.get("event_id", row.get("raw_event_id", ""))).strip()
        candidate_id = str(row.get("candidate_id", "")).strip()
        content_hash = str(row.get("content_hash", "")).strip()
        canonical_url = str(row.get("canonical_url", "")).strip()
        if not event_id or event_id in published_ids or candidate_id in published_ids:
            continue
        if content_hash and content_hash in published_hashes:
            continue
        if canonical_url and canonical_url in published_urls:
            continue
        prior = unique.get(event_id)
        if prior is None or _sort_key(row, now=now) > _sort_key(prior, now=now):
            unique[event_id] = row

    ordered = sorted(unique.values(), key=lambda row: _sort_key(row, now=now), reverse=True)
    return ordered[:limit]


def _sort_key(row: Mapping[str, Any], *, now: datetime) -> tuple[float, float, str]:
    try:
        score = float(row.get("editorial_score", row.get("confidence", 0)) or 0)
    except (TypeError, ValueError):
        score = 0.0
    published = _aware_datetime(row.get("published_at"))
    if published is None:
        recency = 0.0
    else:
        hours = max(0.0, (now - published.astimezone(UTC)).total_seconds() / 3600)
        recency = max(0.0, 10.0 - min(10.0, hours / 7.2))
    stable_id = str(row.get("event_id", row.get("candidate_id", "")))
    return score, recency, stable_id


def _aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = date_parser.isoparse(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


__all__ = ["rank_eligible_events"]
