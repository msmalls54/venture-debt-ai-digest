from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from vdai.agentmail import DeliveryReceipt
from vdai.collector import JobCollectionResult
from vdai.config import AppSettings
from vdai.models import (
    CandidateV1,
    DigestRunStatus,
    DigestSection,
    Edition,
    EventStatus,
    EventV1,
    EvidenceDecision,
    Fact,
    HealthStatus,
    Priority,
    Recipient,
    RecipientBatch,
    SourceConfig,
    SourceHealth,
    SourceTier,
)
from vdai.orchestrator import (
    DigestOrchestrator,
    _analysis_window_start,
    _candidate_is_fresh,
    _candidate_is_relevant,
    _prioritize_candidates,
    _research_seed_headlines,
)

NOW = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)  # Monday, 7:00 AM Pacific


def _source(source_id: str, section: DigestSection) -> SourceConfig:
    return SourceConfig(
        source_id=source_id,
        section=section,
        organization=source_id,
        source_name=f"{source_id} official",
        url=f"https://{source_id.lower()}.example.com/news",
        method="html",
        cadence="15 minutes",
        source_tier=SourceTier.A,
        priority="P0",
        active=True,
        allowed_domains=(f"{source_id.lower()}.example.com",),
        primary_source=True,
        discovery_only=False,
        parser_version="html.v1",
    )


def _health(source: SourceConfig) -> SourceHealth:
    return SourceHealth(
        source_id=source.source_id,
        source_job_key=source.source_id,
        last_attempt_at=NOW,
        last_success_at=NOW,
        status=HealthStatus.SUCCESS,
        parser_version=source.parser_version,
        updated_at=NOW,
        run_id="RUN-prior",
        source_name=source.source_name,
        checked_at=NOW,
        failure_stage="source_complete",
    )


def _event() -> EventV1:
    return EventV1(
        event_id="event-ai-1",
        candidate_id="candidate-ai-1",
        run_id="collector-run",
        source_id="S003",
        source_job_key="S003",
        decision=EvidenceDecision.KEEP,
        section=DigestSection.AI_RADAR,
        event_type="model_launch",
        event_status=EventStatus.LAUNCHED,
        company="Signal AI",
        evidence_tier=SourceTier.A,
        facts=(
            Fact(
                fact_id="fact-ai-1",
                claim="Signal AI launched Model Three.",
                exact_quote="Signal AI launched Model Three.",
                source_id="S003",
                source_url="https://s003.example.com/model-three",
                evidence_tier=SourceTier.A,
            ),
        ),
        source_ids=("S003",),
        confidence=96,
        editorial_score=80,
        analyzed_at=NOW,
    )


class _Repository:
    def __init__(self) -> None:
        self.sources = [
            _source("S001", DigestSection.DEAL_TAPE),
            _source("S002", DigestSection.COMPETITIVE_FIELD),
            _source("S003", DigestSection.AI_RADAR),
            _source("S004", DigestSection.RUNWAY_WATCH),
        ]
        self.health = [_health(source) for source in self.sources]
        self.digest_runs: list[Any] = []
        self.published: list[tuple[Any, DigestRunStatus]] = []
        self.sent_ids: set[str] = set()
        self.ready_editions: dict[str, Edition] = {}

    def load_sources(self, *, active_only: bool = True) -> list[SourceConfig]:
        return self.sources

    def load_settings(self) -> dict[str, Any]:
        return {
            "test_mode": False,
            "agentmail_send_enabled": True,
            "p0_source_health_threshold": 0.9,
            "lookback_hours": 30,
            "dedupe_window_days": 7,
        }

    def load_company_watchlist(self) -> list[dict[str, Any]]:
        return []

    def load_deposit_watch(self) -> list[dict[str, Any]]:
        return []

    def load_source_health(self, **_kwargs: Any) -> list[SourceHealth]:
        return self.health

    def load_last_sent_at(self) -> datetime | None:
        return None

    def load_latest_health_by_job(self) -> dict[str, SourceHealth]:
        return {receipt.source_job_key: receipt for receipt in self.health}

    def load_recipients(self, **_kwargs: Any) -> RecipientBatch:
        return RecipientBatch(recipients=(Recipient(email="reader@example.com"),))

    def candidate_exists(self, _candidate: Any) -> bool:
        return False

    def load_candidate_index(self, **_kwargs: Any) -> tuple[set[str], set[str]]:
        return set(), set()

    def append_source_health(self, receipt: SourceHealth) -> None:
        self.health.append(receipt)

    def append_source_health_many(self, receipts: list[SourceHealth]) -> None:
        self.health.extend(receipts)

    def upsert_raw_event(self, _event: Any, _candidate: Any) -> None:
        raise AssertionError("the valid-empty collection should not create an event")

    def upsert_raw_events(self, items: list[Any], **_kwargs: Any) -> None:
        assert items == []

    def digest_was_sent(self, send_id: str) -> bool:
        return send_id in self.sent_ids

    def load_ready_edition(self, send_id: str) -> Edition | None:
        return self.ready_editions.get(send_id)

    def load_eligible_events(self, **_kwargs: Any) -> list[dict[str, Any]]:
        row = _event().model_dump(mode="python")
        row["source_name"] = "Signal AI official"
        return [row]

    def load_published_history(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "raw_event_id": story.raw_event_id,
                "candidate_id": story.candidate_id,
                "canonical_url": str(story.citations[0].url),
                "history_at": NOW,
            }
            for story, status in self.published
            if status is DigestRunStatus.SENT
        ]

    def upsert_published_story(
        self, story: Any, _edition: Any, *, status: DigestRunStatus, **_kwargs: Any
    ) -> None:
        self.published.append((story, status))

    def upsert_published_stories(
        self, stories: Any, _edition: Any, *, status: DigestRunStatus, **_kwargs: Any
    ) -> None:
        self.published.extend((story, status) for story in stories)

    def upsert_digest_run(self, run: Any) -> None:
        self.digest_runs.append(run)
        if run.status is DigestRunStatus.READY_TO_SEND:
            self.ready_editions[run.send_id] = Edition.model_validate(run.edition_json)
        if run.status is DigestRunStatus.SENT:
            self.sent_ids.add(run.send_id)


class _Collector:
    def __init__(self) -> None:
        self.job_keys: list[str] = []

    async def collect(self, jobs: Any, *, run_id: str) -> tuple[JobCollectionResult, ...]:
        del run_id
        job_list = list(jobs)
        self.job_keys.extend(job.source_job_key for job in job_list)
        return tuple(JobCollectionResult(source_job_key=job.source_job_key) for job in job_list)


class _Evidence:
    async def analyze(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("no new candidate should reach evidence analysis")


class _Editor:
    def __init__(self) -> None:
        self.calls = 0

    async def draft(
        self, events: list[dict[str, Any]], *, edition_date: str, **_kwargs: Any
    ) -> dict[str, Any]:
        self.calls += 1
        assert "reader@example.com" not in repr(events)
        return {
            "edition_date": edition_date,
            "edition": {
                "kicker": "VENTURE DEBT + AI",
                "headline": "Signal AI makes its move",
                "deck": "One material release cleared the bar.",
            },
            "lead": None,
            "deal_tape": [],
            "competitive_field": [],
            "ai_radar": [
                {
                    "event_id": "event-ai-1",
                    "section": "ai_radar",
                    "headline": "Signal AI — Model Three launches",
                    "dek": "Signal AI launched Model Three.",
                    "why_it_matters": "A credible new model adds pressure to the field.",
                    "fact_ids": ["fact-ai-1"],
                    "source_ids": ["S003"],
                }
            ],
            "runway_watch": [],
            "quiet_day": False,
            "validated_stories": [
                {
                    "event_id": "event-ai-1",
                    "section": "ai_radar",
                    "headline": "Signal AI — Model Three launches",
                    "dek": "Signal AI launched Model Three.",
                    "why_it_matters": "A credible new model adds pressure to the field.",
                    "fact_ids": ["fact-ai-1"],
                    "source_ids": ["S003"],
                }
            ],
            "story_count": 1,
            "word_count": 30,
        }


class _AgentMail:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.bodies: list[tuple[str, str, str]] = []

    async def deliver(self, rendered: Any, *, recipients: list[str], send_id: str) -> Any:
        self.calls += 1
        self.bodies.append((rendered.subject, rendered.html, rendered.text))
        assert recipients == ["reader@example.com"]
        if self.fail_first and self.calls == 1:
            raise RuntimeError("simulated provider failure")
        return DeliveryReceipt(
            sent=True,
            status="ACCEPTED",
            send_id=send_id,
            recipient_count=1,
            message_id="msg-1",
            thread_id="thread-1",
            accepted_at=NOW.isoformat(),
        )


async def test_orchestrator_sends_once_and_records_provider_receipt() -> None:
    repository = _Repository()
    editor = _Editor()
    agentmail = _AgentMail()
    settings = AppSettings(
        google_sheet_id="test-google-sheet-id",
        test_mode=False,
        agentmail_send_enabled=True,
    )
    orchestrator = DigestOrchestrator(
        settings=settings,
        repository=repository,  # type: ignore[arg-type]
        collector=_Collector(),  # type: ignore[arg-type]
        evidence=_Evidence(),  # type: ignore[arg-type]
        editor=editor,  # type: ignore[arg-type]
        agentmail=agentmail,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    first = await orchestrator.run_once(manual_source_id="S001")
    second = await orchestrator.run_once(manual_source_id="S001")

    assert first.delivery_status == "ACCEPTED"
    assert first.edition_story_count == 1
    assert second.delivery_status == "ALREADY_SENT"
    assert editor.calls == 1
    assert agentmail.calls == 1
    assert [run.status for run in repository.digest_runs] == [
        DigestRunStatus.READY_TO_SEND,
        DigestRunStatus.SENT,
    ]
    assert repository.digest_runs[-1].message_id == "msg-1"
    assert repository.digest_runs[-1].thread_id == "thread-1"
    assert [status for _, status in repository.published] == [DigestRunStatus.SENT]
    assert orchestrator.last_rendered is not None
    assert "background:#050706" in orchestrator.last_rendered.html
    assert "linear-gradient(#050706,#050706)" in orchestrator.last_rendered.html
    assert "VENTURE DEBT + AI BRIEF" in orchestrator.last_rendered.html


async def test_explicit_revision_never_republishes_a_same_day_story() -> None:
    repository = _Repository()
    editor = _Editor()
    agentmail = _AgentMail()
    orchestrator = DigestOrchestrator(
        settings=AppSettings(
            google_sheet_id="test-google-sheet-id",
            test_mode=False,
            agentmail_send_enabled=True,
        ),
        repository=repository,  # type: ignore[arg-type]
        collector=_Collector(),  # type: ignore[arg-type]
        evidence=_Evidence(),  # type: ignore[arg-type]
        editor=editor,  # type: ignore[arg-type]
        agentmail=agentmail,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    original = await orchestrator.run_once(manual_source_id="S001")
    update = await orchestrator.run_once(
        send_once=True,
        require_story=True,
        full_brief=True,
        send_revision="eod-v2",
        manual_source_id="S001",
    )

    assert original.delivery_status == "ACCEPTED"
    assert update.delivery_status == "HELD_NO_MATERIAL_STORIES"
    assert update.send_id == "vdai-2026-08-24-daily-eod-v2"
    assert update.edition_story_count == 0
    assert agentmail.calls == 1


async def test_scheduled_delivery_morning_forces_a_fresh_full_source_sweep() -> None:
    repository = _Repository()
    collector = _Collector()
    orchestrator = DigestOrchestrator(
        settings=AppSettings(
            google_sheet_id="test-google-sheet-id",
            test_mode=False,
            agentmail_send_enabled=False,
        ),
        repository=repository,  # type: ignore[arg-type]
        collector=collector,  # type: ignore[arg-type]
        evidence=_Evidence(),  # type: ignore[arg-type]
        editor=_Editor(),  # type: ignore[arg-type]
        agentmail=_AgentMail(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    result = await orchestrator.run_once(collect_only=True)

    assert result.due_job_count == len(repository.sources) == 4
    assert collector.job_keys == [source.source_id for source in repository.sources]


async def test_scheduled_full_sweep_still_respects_active_quarantine() -> None:
    repository = _Repository()
    repository.health[0] = repository.health[0].model_copy(
        update={"quarantined_until": NOW + timedelta(hours=1)}
    )
    collector = _Collector()
    orchestrator = DigestOrchestrator(
        settings=AppSettings(
            google_sheet_id="test-google-sheet-id",
            test_mode=False,
            agentmail_send_enabled=False,
        ),
        repository=repository,  # type: ignore[arg-type]
        collector=collector,  # type: ignore[arg-type]
        evidence=_Evidence(),  # type: ignore[arg-type]
        editor=_Editor(),  # type: ignore[arg-type]
        agentmail=_AgentMail(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    result = await orchestrator.run_once(collect_only=True)

    assert result.due_job_count == 3
    assert collector.job_keys == ["S002", "S003", "S004"]


async def test_explicit_full_preview_forces_a_fresh_full_source_sweep() -> None:
    repository = _Repository()
    off_hour = datetime(2026, 8, 24, 19, 0, tzinfo=UTC)  # Noon Pacific.
    repository.health = [
        receipt.model_copy(
            update={
                "last_attempt_at": off_hour,
                "last_success_at": off_hour,
                "checked_at": off_hour,
                "updated_at": off_hour,
            }
        )
        for receipt in repository.health
    ]
    collector = _Collector()
    orchestrator = DigestOrchestrator(
        settings=AppSettings(
            google_sheet_id="test-google-sheet-id",
            test_mode=False,
            agentmail_send_enabled=False,
        ),
        repository=repository,  # type: ignore[arg-type]
        collector=collector,  # type: ignore[arg-type]
        evidence=_Evidence(),  # type: ignore[arg-type]
        editor=_Editor(),  # type: ignore[arg-type]
        agentmail=_AgentMail(),  # type: ignore[arg-type]
        clock=lambda: off_hour,
    )

    result = await orchestrator.run_once(
        dry_run=True,
        collect_only=True,
        force_editor=True,
        full_brief=True,
    )

    assert result.due_job_count == len(repository.sources) == 4
    assert collector.job_keys == [source.source_id for source in repository.sources]


async def test_orchestrator_dry_run_never_writes_or_sends() -> None:
    repository = _Repository()
    agentmail = _AgentMail()
    settings = AppSettings(
        google_sheet_id="test-google-sheet-id",
        test_mode=False,
        agentmail_send_enabled=True,
    )
    orchestrator = DigestOrchestrator(
        settings=settings,
        repository=repository,  # type: ignore[arg-type]
        collector=_Collector(),  # type: ignore[arg-type]
        evidence=_Evidence(),  # type: ignore[arg-type]
        editor=_Editor(),  # type: ignore[arg-type]
        agentmail=agentmail,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    result = await orchestrator.run_once(
        dry_run=True,
        force_editor=True,
        allow_degraded_preview=True,
        manual_source_id="S001",
    )

    assert result.delivery_status == "DRY_RUN_RENDERED"
    assert repository.digest_runs == []
    assert repository.published == []
    assert agentmail.calls == 0


async def test_agentmail_retry_reuses_exact_persisted_ready_edition() -> None:
    repository = _Repository()
    editor = _Editor()
    agentmail = _AgentMail(fail_first=True)
    settings = AppSettings(
        google_sheet_id="test-google-sheet-id",
        test_mode=False,
        agentmail_send_enabled=True,
    )
    orchestrator = DigestOrchestrator(
        settings=settings,
        repository=repository,  # type: ignore[arg-type]
        collector=_Collector(),  # type: ignore[arg-type]
        evidence=_Evidence(),  # type: ignore[arg-type]
        editor=editor,  # type: ignore[arg-type]
        agentmail=agentmail,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    failed = await orchestrator.run_once(manual_source_id="S001")
    recovered = await orchestrator.run_once(manual_source_id="S001")

    assert failed.delivery_status == "FAILED_CLOSED_AGENTMAIL"
    assert recovered.delivery_status == "ACCEPTED"
    assert editor.calls == 1
    assert agentmail.calls == 2
    assert agentmail.bodies[0] == agentmail.bodies[1]
    assert [run.status for run in repository.digest_runs] == [
        DigestRunStatus.READY_TO_SEND,
        DigestRunStatus.FAILED_CLOSED,
        DigestRunStatus.SENT,
    ]
    assert [status for _, status in repository.published] == [DigestRunStatus.SENT]


async def test_force_editor_is_preview_only_even_when_live_gates_are_enabled() -> None:
    repository = _Repository()
    agentmail = _AgentMail()
    settings = AppSettings(
        google_sheet_id="test-google-sheet-id",
        test_mode=False,
        agentmail_send_enabled=True,
    )
    orchestrator = DigestOrchestrator(
        settings=settings,
        repository=repository,  # type: ignore[arg-type]
        collector=_Collector(),  # type: ignore[arg-type]
        evidence=_Evidence(),  # type: ignore[arg-type]
        editor=_Editor(),  # type: ignore[arg-type]
        agentmail=agentmail,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    result = await orchestrator.run_once(force_editor=True, manual_source_id="S001")

    assert result.delivery_status == "HELD_FORCED_PREVIEW"
    assert agentmail.calls == 0
    assert [run.status for run in repository.digest_runs] == [DigestRunStatus.HELD]


async def test_send_once_bypasses_only_the_clock_and_remains_idempotent() -> None:
    repository = _Repository()
    agentmail = _AgentMail()
    off_hour = datetime(2026, 8, 25, 16, 0, tzinfo=UTC)  # 9:00 AM Pacific
    settings = AppSettings(
        google_sheet_id="test-google-sheet-id",
        test_mode=False,
        agentmail_send_enabled=True,
    )
    orchestrator = DigestOrchestrator(
        settings=settings,
        repository=repository,  # type: ignore[arg-type]
        collector=_Collector(),  # type: ignore[arg-type]
        evidence=_Evidence(),  # type: ignore[arg-type]
        editor=_Editor(),  # type: ignore[arg-type]
        agentmail=agentmail,  # type: ignore[arg-type]
        clock=lambda: off_hour,
    )

    first = await orchestrator.run_once(send_once=True, require_story=True)
    second = await orchestrator.run_once(send_once=True, require_story=True)

    assert first.delivery_status == "ACCEPTED"
    assert second.delivery_status == "ALREADY_SENT"
    assert agentmail.calls == 1


def test_candidate_prefilter_caps_calls_and_balances_operating_lanes() -> None:
    candidates = []
    for lane_index, section in enumerate(
        (
            DigestSection.DEAL_TAPE,
            DigestSection.COMPETITIVE_FIELD,
            DigestSection.AI_RADAR,
        )
    ):
        for source_index in range(4):
            source_id = f"S{lane_index}{source_index}"
            for item_index in range(3):
                identity = f"{section.value}-{source_index}-{item_index}"
                candidates.append(
                    CandidateV1.model_construct(
                        candidate_id=identity,
                        source_id=source_id,
                        source_job_key=source_id,
                        canonical_url=f"https://example.com/{identity}",
                        content_sha256=f"{len(candidates):064x}",
                        priority=Priority.P0,
                        published_at=NOW,
                        watchlist_hits=(),
                        primary_source=True,
                        section_hint=section,
                    )
                )

    selected = _prioritize_candidates(candidates, limit=12)

    assert len(selected) == 12
    assert sum(item.section_hint is DigestSection.DEAL_TAPE for item in selected) == 4
    assert sum(item.section_hint is DigestSection.COMPETITIVE_FIELD for item in selected) == 4
    assert sum(item.section_hint is DigestSection.AI_RADAR for item in selected) == 4
    assert len({item.source_id for item in selected}) == 12
    assert max(Counter(item.source_id for item in selected).values()) == 1
    assert max(Counter(item.source_job_key for item in selected).values()) == 1


def test_candidate_prefilter_reserves_eight_calls_for_web_research() -> None:
    candidates = []
    sections = (
        DigestSection.DEAL_TAPE,
        DigestSection.COMPETITIVE_FIELD,
        DigestSection.AI_RADAR,
    )
    for item_index in range(30):
        source_id = f"S{item_index:03d}"
        candidates.append(
            CandidateV1.model_construct(
                candidate_id=f"registry-{item_index}",
                source_id=source_id,
                source_job_key=source_id,
                canonical_url=f"https://example.com/registry/{item_index}",
                content_sha256=f"{item_index:064x}",
                priority=Priority.P0,
                published_at=NOW,
                watchlist_hits=(),
                primary_source=True,
                section_hint=sections[item_index % len(sections)],
            )
        )
    for item_index in range(8):
        source_id = f"WEB-{item_index:03d}"
        candidates.append(
            CandidateV1.model_construct(
                candidate_id=f"web-{item_index}",
                source_id=source_id,
                source_job_key=f"{source_id}:research",
                canonical_url=f"https://news.example.com/web/{item_index}",
                content_sha256=f"{item_index + 100:064x}",
                priority=Priority.P1,
                published_at=NOW,
                watchlist_hits=(),
                primary_source=False,
                section_hint=sections[item_index % len(sections)],
            )
        )

    selected = _prioritize_candidates(candidates, limit=24)

    assert len(selected) == 24
    assert sum(item.source_id.startswith("WEB-") for item in selected) == 8
    assert sum(not item.source_id.startswith("WEB-") for item in selected) == 16


def test_candidate_prefilter_exhausts_distinct_sources_before_second_items() -> None:
    candidates = []
    sections = (
        DigestSection.DEAL_TAPE,
        DigestSection.COMPETITIVE_FIELD,
        DigestSection.AI_RADAR,
    )
    for source_index in range(45):
        source_id = f"S{source_index:03d}"
        for item_index in range(2):
            candidates.append(
                CandidateV1.model_construct(
                    candidate_id=f"registry-{source_index}-{item_index}",
                    source_id=source_id,
                    source_job_key=source_id,
                    canonical_url=(
                        f"https://example.com/registry/{source_index}/{item_index}"
                    ),
                    content_sha256=f"{source_index * 2 + item_index:064x}",
                    priority=Priority.P0,
                    published_at=NOW,
                    watchlist_hits=(),
                    primary_source=True,
                    discovery_only=False,
                    source_class=SourceTier.A,
                    section_hint=sections[source_index % len(sections)],
                )
            )
    for item_index in range(8):
        candidates.append(
            CandidateV1.model_construct(
                candidate_id=f"web-{item_index}",
                source_id="WEB-RESEARCH",
                source_job_key="WEB-RESEARCH",
                canonical_url=f"https://news.example.com/web/{item_index}",
                content_sha256=f"{200 + item_index:064x}",
                priority=Priority.P1,
                published_at=NOW,
                watchlist_hits=(),
                primary_source=False,
                discovery_only=True,
                source_class=SourceTier.C,
                section_hint=sections[item_index % len(sections)],
            )
        )

    selected = _prioritize_candidates(candidates, limit=40)

    assert len(selected) == 40
    assert len({item.source_id for item in selected}) == 40
    assert max(Counter(item.source_id for item in selected).values()) == 1


def test_candidate_prefilter_reserves_four_calls_for_citable_secondary_debt() -> None:
    candidates = []
    for item_index in range(30):
        source_id = f"S{item_index:03d}"
        candidates.append(
            CandidateV1.model_construct(
                candidate_id=f"primary-{item_index}",
                source_id=source_id,
                source_job_key=source_id,
                canonical_url=f"https://example.com/primary/{item_index}",
                content_sha256=f"{item_index:064x}",
                priority=Priority.P0,
                published_at=NOW,
                watchlist_hits=(),
                primary_source=True,
                discovery_only=False,
                source_class=SourceTier.A,
                section_hint=DigestSection.AI_RADAR,
            )
        )
    for item_index in range(6):
        source_id = f"DEBT-{item_index:03d}"
        candidates.append(
            CandidateV1.model_construct(
                candidate_id=f"secondary-debt-{item_index}",
                source_id=source_id,
                source_job_key=source_id,
                canonical_url=f"https://credit.example.com/deal/{item_index}",
                content_sha256=f"{item_index + 100:064x}",
                priority=Priority.P1,
                published_at=NOW,
                watchlist_hits=(),
                primary_source=False,
                discovery_only=False,
                source_class=SourceTier.C,
                section_hint=DigestSection.DEAL_TAPE,
            )
        )

    selected = _prioritize_candidates(candidates, limit=24)

    assert len(selected) == 24
    assert sum(item.candidate_id.startswith("secondary-debt-") for item in selected) == 4


def test_candidate_prefilter_prefers_concrete_debt_facility_over_generic_update() -> None:
    candidates = [
        CandidateV1.model_construct(
            candidate_id="generic",
            source_id="S-GENERIC",
            source_job_key="S-GENERIC",
            canonical_url="https://example.com/generic",
            content_sha256="1" * 64,
            priority=Priority.P0,
            published_at=NOW,
            watchlist_hits=(),
            primary_source=True,
            discovery_only=False,
            source_class=SourceTier.A,
            title="Lender announces quarterly platform update",
            summary_text="A general platform update.",
            section_hint=DigestSection.DEAL_TAPE,
        ),
        CandidateV1.model_construct(
            candidate_id="facility",
            source_id="S-FACILITY",
            source_job_key="S-FACILITY",
            canonical_url="https://example.com/facility",
            content_sha256="2" * 64,
            priority=Priority.P0,
            published_at=NOW - timedelta(days=2),
            watchlist_hits=(),
            primary_source=True,
            discovery_only=False,
            source_class=SourceTier.A,
            title="Borrower closes $100 million senior secured financing facility",
            summary_text="The term loan was led by an innovation bank.",
            section_hint=DigestSection.DEAL_TAPE,
        ),
    ]

    selected = _prioritize_candidates(candidates, limit=1)

    assert [candidate.candidate_id for candidate in selected] == ["facility"]


def test_editorial_runs_use_full_recovery_window_after_recent_send() -> None:
    last_sent = NOW - timedelta(hours=1)

    assert _analysis_window_start(
        NOW,
        last_sent,
        editor_requested=True,
        full_brief=False,
    ) == NOW - timedelta(days=8)
    assert _analysis_window_start(
        NOW,
        last_sent,
        editor_requested=False,
        full_brief=False,
    ) == last_sent
    assert _analysis_window_start(
        NOW,
        None,
        editor_requested=False,
        full_brief=False,
    ) == NOW - timedelta(hours=96)


def test_research_seeds_prioritize_transaction_discovery_headlines() -> None:
    candidates = [
        CandidateV1.model_construct(
            candidate_id="primary-model",
            title="Routine model documentation update",
            discovery_only=False,
            published_at=NOW,
            watchlist_hits=(),
        ),
        CandidateV1.model_construct(
            candidate_id="discovery-opinion",
            title="Opinion: what markets may do next",
            discovery_only=True,
            published_at=NOW,
            watchlist_hits=(),
        ),
        CandidateV1.model_construct(
            candidate_id="discovery-deal",
            title="Aerospace startup closes $100M senior secured financing facility - Wire",
            discovery_only=True,
            published_at=NOW - timedelta(days=2),
            watchlist_hits=(),
        ),
    ]

    selected = _research_seed_headlines(candidates, limit=2)

    assert [candidate.candidate_id for candidate in selected] == [
        "discovery-deal",
        "discovery-opinion",
    ]


def test_relevance_prefilter_defers_judgment_to_gemini_except_explicit_noise() -> None:
    def candidate(source_id: str, text: str) -> CandidateV1:
        return CandidateV1.model_construct(
            source_id=source_id,
            title=text,
            summary_text="",
            body_text=text,
        )

    assert _candidate_is_relevant(
        candidate("S064", "Amazon Bedrock launches a new foundation model capability")
    )
    assert _candidate_is_relevant(
        candidate("S064", "AWS announces a routine storage availability-zone update")
    )
    assert _candidate_is_relevant(
        candidate("S127", "EIB provides venture debt financing for a deep-tech company")
    )
    assert _candidate_is_relevant(candidate("S127", "EIB finances a municipal road-repair program"))
    assert _candidate_is_relevant(
        candidate("S126", "Vistara Growth announces a new portfolio investment")
    )
    assert _candidate_is_relevant(candidate("S126", "Vistara Growth appoints a new vice president"))
    assert _candidate_is_relevant(
        candidate("S128", "KKR Expands Global Credit & Markets Platform with Senior Hires")
    )
    assert not _candidate_is_relevant(
        candidate("S128", "KKR reports quarterly results and declares a dividend")
    )
    assert _candidate_is_relevant(
        candidate(
            "S129",
            "Moelis Appoints Russell Mason as a Managing Director in its Capital "
            "Structure Advisory Group",
        )
    )
    assert not _candidate_is_relevant(
        candidate("S129", "Moelis CEO to speak at an investor conference")
    )
    assert _candidate_is_relevant(
        candidate("S135", "Brex launches a new treasury cash-management product")
    )
    assert _candidate_is_relevant(
        candidate("S150", "Square launches a 3.50% APY high-yield savings account")
    )
    assert not _candidate_is_relevant(
        candidate("S135", "Brex opens a redesigned office in San Francisco")
    )
    assert not _candidate_is_relevant(
        candidate("S141", "Global Relay completes an office acquisition")
    )
    assert _candidate_is_relevant(
        candidate("S132", "Bluevine expands its small-business credit product")
    )
    assert not _candidate_is_relevant(
        candidate("S132", "Bluevine publishes a guide to hiring interns")
    )
    assert _candidate_is_relevant(
        candidate("S171", "Blackstone Credit provides a new software financing facility")
    )
    assert not _candidate_is_relevant(
        candidate("S171", "Blackstone names a new real-estate board member")
    )
    assert _candidate_is_relevant(
        candidate("S212", "Startup raises a $45 million Series B funding round")
    )
    assert not _candidate_is_relevant(
        candidate("S212", "Founder shares five lessons from building a remote team")
    )
    assert _candidate_is_relevant(
        candidate("S208", "Gemini model abuse creates a new cyber threat")
    )
    assert not _candidate_is_relevant(candidate("S208", "Google opens a new cloud region"))
    assert _candidate_is_relevant(candidate("S209", "A Science of Scheming"))
    assert _candidate_is_relevant(candidate("S224", "Revolut nabs conditional OCC charter"))
    assert _candidate_is_relevant(
        candidate("S224", "Banks without holding companies need better disclosure rules")
    )
    assert _candidate_is_relevant(candidate("S999", "Unscoped sources remain unchanged"))


def test_scheduled_send_window_is_monday_wednesday_friday_at_seven_pacific() -> None:
    settings = AppSettings(
        google_sheet_id="test-google-sheet-id",
        timezone="America/Los_Angeles",
        send_hour_local=7,
        send_minute_window=60,
    )
    orchestrator = DigestOrchestrator(
        settings=settings,
        repository=object(),  # type: ignore[arg-type]
        collector=object(),  # type: ignore[arg-type]
        evidence=object(),  # type: ignore[arg-type]
        editor=object(),  # type: ignore[arg-type]
        agentmail=object(),  # type: ignore[arg-type]
    )

    for send_day in (
        datetime(2026, 8, 31, 14, 0, tzinfo=UTC),  # Monday
        datetime(2026, 9, 2, 14, 0, tzinfo=UTC),  # Wednesday
        datetime(2026, 9, 4, 14, 0, tzinfo=UTC),  # Friday
    ):
        assert orchestrator._inside_send_window(send_day) is True

    for collection_only_day in (
        datetime(2026, 9, 1, 14, 0, tzinfo=UTC),  # Tuesday
        datetime(2026, 9, 3, 14, 0, tzinfo=UTC),  # Thursday
        datetime(2026, 9, 5, 14, 0, tzinfo=UTC),  # Saturday
        datetime(2026, 9, 6, 14, 0, tzinfo=UTC),  # Sunday
    ):
        assert orchestrator._inside_send_window(collection_only_day) is False

    assert orchestrator._inside_send_window(datetime(2026, 8, 31, 16, 0, tzinfo=UTC)) is False


def test_candidate_freshness_uses_source_date_and_allows_first_seen_undated_pages() -> None:
    recent = CandidateV1.model_construct(
        published_at=NOW - timedelta(hours=12),
        retrieved_at=NOW,
    )
    stale = CandidateV1.model_construct(
        published_at=NOW - timedelta(days=10),
        retrieved_at=NOW,
    )
    undated = CandidateV1.model_construct(
        published_at=None,
        retrieved_at=NOW,
        title="Current release notes",
    )
    dated_title_stale = CandidateV1.model_construct(
        published_at=None,
        retrieved_at=NOW,
        title="June 18, 2026",
    )
    archive_item = CandidateV1.model_construct(
        published_at=None,
        retrieved_at=NOW,
        title="Evergreen archive item",
    )
    malformed = CandidateV1.model_construct(
        published_at=None,
        retrieved_at=NOW,
        title="<!DOCTYPE html><html>",
    )
    cutoff = NOW - timedelta(hours=96)

    assert _candidate_is_fresh(recent, cutoff=cutoff, now=NOW) is True
    assert _candidate_is_fresh(stale, cutoff=cutoff, now=NOW) is False
    assert _candidate_is_fresh(undated, cutoff=cutoff, now=NOW) is True
    assert _candidate_is_fresh(dated_title_stale, cutoff=cutoff, now=NOW) is False
    assert (
        _candidate_is_fresh(
            archive_item,
            cutoff=cutoff,
            now=NOW,
            source_job_item_count=20,
        )
        is False
    )
    assert _candidate_is_fresh(malformed, cutoff=cutoff, now=NOW) is False
