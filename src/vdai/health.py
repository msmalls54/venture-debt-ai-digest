"""Deterministic source-health gates for the daily edition."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import DigestSection, HealthStatus, SourceHealth, SourceJob

HEALTHY_STATUSES = frozenset(
    {HealthStatus.SUCCESS, HealthStatus.UNCHANGED, HealthStatus.VALID_EMPTY}
)
REQUIRED_LANES = ("venture_debt", "competitor_deposit", "ai")


@dataclass(frozen=True, slots=True)
class HealthGateResult:
    healthy: bool
    healthy_jobs: int
    expected_jobs: int
    healthy_ratio: float
    covered_lanes: tuple[str, ...]
    missing_lanes: tuple[str, ...]
    reason_codes: tuple[str, ...]
    collector_fresh: bool

    @property
    def p0_healthy(self) -> int:
        """Backward-compatible alias for older receipt readers."""

        return self.healthy_jobs

    @property
    def p0_expected(self) -> int:
        return self.expected_jobs

    @property
    def p0_ratio(self) -> float:
        return self.healthy_ratio

    @property
    def covered_sections(self) -> tuple[str, ...]:
        return self.covered_lanes

    @property
    def missing_sections(self) -> tuple[str, ...]:
        return self.missing_lanes


def evaluate_health_gate(
    all_jobs: Iterable[SourceJob],
    due_jobs: Iterable[SourceJob],
    latest_health: Mapping[str, SourceHealth],
    *,
    threshold: float,
    now: datetime,
    collector_completed_at: datetime | None,
    collector_freshness: timedelta = timedelta(minutes=90),
    section_freshness: timedelta = timedelta(hours=36),
) -> HealthGateResult:
    """Require healthy launch-core coverage across the three operating lanes."""

    if now.tzinfo is None:
        raise ValueError("health gate requires a timezone-aware clock")
    if not 0 < threshold <= 1:
        raise ValueError("health threshold must be between zero and one")
    if collector_freshness <= timedelta(0) or section_freshness <= timedelta(0):
        raise ValueError("health freshness windows must be positive")
    collector_fresh = (
        collector_completed_at is not None
        and collector_completed_at.tzinfo is not None
        and timedelta(0) <= now - collector_completed_at <= collector_freshness
    )

    all_job_list = list(all_jobs)
    due_job_list = list(due_jobs)
    scope = due_job_list or all_job_list
    healthy_jobs = sum(
        _job_is_healthy(job, latest_health, now=now, freshness=section_freshness)
        for job in scope
    )
    expected_jobs = len(scope)
    healthy_ratio = healthy_jobs / expected_jobs if expected_jobs else 0.0

    lane_coverage = {lane: False for lane in REQUIRED_LANES}
    for job in all_job_list:
        lane = _health_lane(job.source.section)
        if _job_is_healthy(
            job, latest_health, now=now, freshness=section_freshness
        ):
            lane_coverage[lane] = True

    covered_lanes = tuple(lane for lane, covered in lane_coverage.items() if covered)
    missing_lanes = tuple(lane for lane, covered in lane_coverage.items() if not covered)
    reasons: list[str] = []
    if not expected_jobs:
        reasons.append("NO_CORE_SOURCE_JOBS")
    elif healthy_ratio < threshold:
        reasons.append("CORE_COVERAGE_BELOW_THRESHOLD")
    if missing_lanes:
        reasons.append("CORE_LANE_SOURCE_MISSING")
    if not collector_fresh:
        reasons.append("COLLECTOR_STALE_OR_MISSING")

    return HealthGateResult(
        healthy=not reasons,
        healthy_jobs=healthy_jobs,
        expected_jobs=expected_jobs,
        healthy_ratio=healthy_ratio,
        covered_lanes=covered_lanes,
        missing_lanes=missing_lanes,
        reason_codes=tuple(reasons),
        collector_fresh=collector_fresh,
    )


def _job_is_healthy(
    job: SourceJob,
    latest: Mapping[str, SourceHealth],
    *,
    now: datetime,
    freshness: timedelta,
) -> bool:
    receipt = latest.get(job.source_job_key)
    return (
        receipt is not None
        and receipt.status in HEALTHY_STATUSES
        and receipt.parser_version == job.source.parser_version
        and timedelta(0) <= now - receipt.checked_at <= freshness
    )


def _health_lane(section: DigestSection) -> str:
    if section is DigestSection.AI_RADAR:
        return "ai"
    if section in {DigestSection.COMPETITIVE_FIELD, DigestSection.DEPOSIT_WATCH}:
        return "competitor_deposit"
    return "venture_debt"


__all__ = ["HEALTHY_STATUSES", "REQUIRED_LANES", "HealthGateResult", "evaluate_health_gate"]
