"""One finite Railway invocation: collect, verify, optionally edit, and exit."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .agentmail import AgentMailClient, DeliveryReceipt, build_send_id
from .branding import build_brand_domains
from .collector import Collector, JobCollectionResult, flatten_candidates
from .config import (
    AppSettings,
    expand_source_jobs,
    latest_health_by_job,
    normalize_list,
    select_due_source_jobs,
)
from .editor import DailyEditor, edition_from_draft
from .evidence import EvidenceAnalyst, event_from_analysis
from .health import HealthGateResult, evaluate_health_gate
from .models import (
    CandidateV1,
    DigestRun,
    DigestRunStatus,
    DigestSection,
    Edition,
    EventV1,
    HealthStatus,
    SourceHealth,
    SourceJob,
    SourceTier,
)
from .ranking import rank_eligible_events
from .renderer import RenderedEmail, render_digest
from .research import WebResearchScout
from .sheets import GoogleSheetsRepository

MAX_EVIDENCE_CALLS = 60
EVIDENCE_POLICY_VERSION = "intelligence-v2"
WEB_RESEARCH_EVIDENCE_RESERVE = 8
DEBT_SECONDARY_EVIDENCE_RESERVE = 4
EVIDENCE_CONCURRENCY = 4
INITIAL_LOOKBACK = timedelta(hours=96)
MAX_LOOKBACK = timedelta(days=8)
SCHEDULED_SEND_WEEKDAYS = frozenset({0, 2, 4})  # Monday, Wednesday, Friday
_TITLE_DATE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
    r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+"
    r"\d{1,2},\s+20\d{2}\b|\b20\d{2}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)

# A small number of intentionally broad feeds need a deterministic relevance gate before
# they can consume an evidence-model call. Each inner tuple is an OR group; every group
# must match. The rules are source-specific so they cannot silently narrow unrelated feeds.
_SOURCE_RELEVANCE_RULES: dict[str, tuple[tuple[str, ...], ...]] = {
    "S014": (
        ("k2 healthventures", "k2 health ventures"),
        (
            "venture debt",
            "credit facility",
            "debt financing",
            "non dilutive financing",
            "secures",
            "secured",
            "funding",
        ),
    ),
    "S023": (
        ("western alliance", "bridge bank"),
        (
            "technology",
            "innovation",
            "life science",
            "startup",
            "venture",
            "liquidity",
            "digital asset",
            "stablecoin",
            "on chain",
        ),
        ("debt", "credit", "loan", "financing", "facility", "platform", "banking"),
    ),
    "S027": (
        ("bmo",),
        ("technology banking", "innovation banking", "venture debt", "credit facility"),
    ),
    "S029": (
        (
            "venture capital",
            "market check",
            "market report",
            "financing",
            "funding",
            "investment",
            "raises",
            "acquisition",
            "deposit",
            "treasury",
            "technology 100 million club",
        ),
    ),
    "S064": (
        (
            "amazon bedrock",
            "bedrock",
            "sagemaker",
            "amazon nova",
            "agentcore",
            "generative ai",
            "foundation model",
        ),
    ),
    "S095": (("benchmark", "model", "compute", "training", "ai", "report", "dataset"),),
    "S117": (
        ("victory park capital", "vpc"),
        ("debt", "credit", "loan", "financing", "investment", "capital solution"),
    ),
    "S119": (
        ("tacora capital", "tacora"),
        ("debt", "credit", "loan", "financing", "capital"),
    ),
    "S120": (
        ("coreweave",),
        (
            "data center",
            "gpu",
            "cloud",
            "financing",
            "debt",
            "partnership",
            "launch",
            "compute",
            "capacity",
            "infrastructure",
            "acquisition",
        ),
    ),
    "S121": (
        ("crusoe",),
        (
            "data center",
            "gpu",
            "cloud",
            "financing",
            "debt",
            "partnership",
            "launch",
            "compute",
            "capacity",
            "infrastructure",
            "acquisition",
        ),
    ),
    "S123": (("fund", "investment", "financing", "venture", "portfolio", "ai", "software"),),
    "S124": (
        ("midcap financial",),
        ("venture finance", "credit facility", "venture debt", "debt financing"),
    ),
    "S125": (
        ("kreos", "blackrock"),
        ("venture debt", "growth debt", "credit", "loan", "financing", "debt"),
    ),
    "S126": (
        (
            "financing",
            "investment",
            "venture debt",
            "growth capital",
            "credit facility",
            "fund",
            "acquisition",
            "exit",
        ),
    ),
    "S127": (
        ("eib", "european investment bank"),
        (
            "venture debt",
            "deep tech",
            "artificial intelligence",
            "ai",
            "semiconductor",
            "quantum",
            "life science",
            "health tech",
            "cancer",
            "biotech",
            "innovation",
            "technology",
        ),
        ("financing", "finance", "loan", "credit", "debt", "funding"),
    ),
    "S128": (
        ("kkr",),
        (
            "credit",
            "capital markets",
            "private capital",
            "financing",
            "debt",
            "direct lending",
            "asset based finance",
            "fund",
            "strategic partnership",
            "capital solution",
        ),
    ),
    "S129": (
        ("moelis",),
        (
            "capital structure",
            "private credit",
            "credit secondaries",
            "debt capital markets",
            "capital markets",
            "private capital advisory",
            "structured products",
            "securitization",
            "restructuring",
            "recapitalization",
            "liability management",
        ),
    ),
}

_FINANCE_EVENT_TERMS = (
    "account",
    "acquisition",
    "acquires",
    "apy",
    "banking",
    "cash management",
    "credit",
    "deposit",
    "facility",
    "funding",
    "launch",
    "lending",
    "loan",
    "merger",
    "partner bank",
    "partnership",
    "payments",
    "raises",
    "savings",
    "treasury",
    "yield",
)
_CREDIT_EVENT_TERMS = (
    "capital solution",
    "credit",
    "debt",
    "direct lending",
    "facility",
    "finance",
    "financing",
    "fund",
    "investment",
    "lending",
    "loan",
)
_FUNDING_EVENT_TERMS = (
    "acquisition",
    "acquires",
    "credit facility",
    "debt",
    "financing",
    "funding",
    "investment",
    "merger",
    "raises",
    "round",
    "series",
    "venture",
)

# These sources are deliberately broad discovery feeds. Unlike official product
# pages, they must satisfy every source-specific OR group before consuming a
# model call. This preserves recall on primary sources while bounding noise.
_SOURCE_RELEVANCE_RULES.update(
    {
        "S039": (("rho",), _FINANCE_EVENT_TERMS),
        "S040": (("airwallex",), _FINANCE_EVENT_TERMS),
        "S063": (("ai", "agent", "copilot", "model"),),
        "S125": (("kreos", "blackrock"), _CREDIT_EVENT_TERMS),
        "S132": (("bluevine",), _FINANCE_EVENT_TERMS),
        "S134": (("lead bank",), _FINANCE_EVENT_TERMS),
        "S135": (("brex",), _FINANCE_EVENT_TERMS),
        "S136": (("mercury",), _FINANCE_EVENT_TERMS),
        "S137": (("ramp",), _FINANCE_EVENT_TERMS),
        "S138": (("revolut",), _FINANCE_EVENT_TERMS),
        "S139": (("chime",), _FINANCE_EVENT_TERMS),
        "S140": (("wise",), _FINANCE_EVENT_TERMS),
        "S141": (("relay",), _FINANCE_EVENT_TERMS),
        "S142": (("novo",), _FINANCE_EVENT_TERMS),
        "S143": (("northone", "north one"), _FINANCE_EVENT_TERMS),
        "S144": (("column",), _FINANCE_EVENT_TERMS),
        "S145": (("coastal community bank", "coastal bank"), _FINANCE_EVENT_TERMS),
        "S146": (("evolve bank",), _FINANCE_EVENT_TERMS),
        "S147": (("treasury prime",), _FINANCE_EVENT_TERMS),
        "S148": (("unit",), _FINANCE_EVENT_TERMS),
        "S149": (("stripe",), _FINANCE_EVENT_TERMS),
        "S150": (("square", "block"), _FINANCE_EVENT_TERMS),
        "S151": (("shopify",), _FINANCE_EVENT_TERMS),
        "S152": (("arc",), _FINANCE_EVENT_TERMS),
        "S153": (("treasure financial",), _FINANCE_EVENT_TERMS),
        "S154": (("vesto",), _FINANCE_EVENT_TERMS),
        "S155": (("modern treasury",), _FINANCE_EVENT_TERMS),
        "S156": (("plaid",), _FINANCE_EVENT_TERMS),
        "S157": (("slash",), _FINANCE_EVENT_TERMS),
        "S158": (("enova", "grasshopper bank"), _FINANCE_EVENT_TERMS),
        "S160": (("orix growth capital",), _CREDIT_EVENT_TERMS),
        "S162": (("structural capital",), _CREDIT_EVENT_TERMS),
        "S163": (("national bank",), _CREDIT_EVENT_TERMS),
        "S164": (("deutsche bank",), _CREDIT_EVENT_TERMS),
        "S165": (("citi",), _CREDIT_EVENT_TERMS),
        "S166": (("wells fargo",), _CREDIT_EVENT_TERMS),
        "S167": (("scotiabank",), _CREDIT_EVENT_TERMS),
        "S170": (("barings",), _CREDIT_EVENT_TERMS),
        "S171": (("blackstone",), _CREDIT_EVENT_TERMS),
        "S172": (("oaktree",), _CREDIT_EVENT_TERMS),
        "S173": (("madryn",), _CREDIT_EVENT_TERMS),
        "S174": (("perceptive",), _CREDIT_EVENT_TERMS),
        "S177": (("clearco",), _CREDIT_EVENT_TERMS),
        "S178": (("pipe",), _CREDIT_EVENT_TERMS),
        "S179": (("wayflyer",), _CREDIT_EVENT_TERMS),
        "S180": (("uncapped",), _CREDIT_EVENT_TERMS),
        "S181": (("liquidity capital",), _CREDIT_EVENT_TERMS),
        "S182": (("canadian business growth fund", "cbgf"), _FUNDING_EVENT_TERMS),
        "S207": (("ai", "agent", "copilot"), ("security", "threat", "vulnerability")),
        "S208": (("ai", "gemini", "model"), ("security", "threat", "attack")),
        "S210": (_FUNDING_EVENT_TERMS,),
        "S211": (_FUNDING_EVENT_TERMS,),
        "S212": (_FUNDING_EVENT_TERMS,),
        "S213": (_FUNDING_EVENT_TERMS,),
        "S214": (_FUNDING_EVENT_TERMS,),
        "S215": (_FUNDING_EVENT_TERMS,),
        "S216": (_FUNDING_EVENT_TERMS,),
        "S217": (_CREDIT_EVENT_TERMS,),
        "S218": (_CREDIT_EVENT_TERMS,),
        "S219": (("bank", "fintech", "merger", "payment", "lending"),),
        "S220": (("bank", "fintech", "merger", "payment", "lending"),),
        "S221": (("bank", "fintech", "payment", "deposit", "lending"),),
        "S222": (("bank", "fintech", "merger", "acquisition", "enforcement"),),
        "S223": (("bank", "fintech", "merger", "acquisition", "enforcement"),),
        "S224": (
            (
                "bank",
                "banking",
                "banks",
                "capital",
                "charter",
                "credit",
                "deposit",
                "fintech",
                "fund",
                "lending",
                "merger",
                "acquisition",
                "partnership",
                "payments",
                "startup",
                "treasury",
            ),
        ),
    }
)
_STRICT_SOURCE_RELEVANCE_IDS = frozenset(
    {
        "S039",
        "S040",
        "S063",
        "S125",
        "S132",
        "S134",
        *(f"S{value:03d}" for value in range(135, 159)),
        "S160",
        *(f"S{value:03d}" for value in range(162, 168)),
        *(f"S{value:03d}" for value in range(170, 175)),
        "S177",
        "S178",
        "S179",
        "S180",
        "S181",
        "S182",
        *(f"S{value:03d}" for value in range(207, 225) if value != 209),
    }
)

_SOURCE_RELEVANCE_EXCLUSIONS: dict[str, tuple[str, ...]] = {
    "S137": ("world bank group ramp",),
    "S141": ("global relay",),
    "S128": (
        "quarterly results",
        "earnings",
        "dividend",
        "to present at",
        "conference webcast",
        "wins award",
    ),
    "S129": (
        "quarterly results",
        "financial results",
        "earnings",
        "dividend",
        "to announce",
        "to speak at",
        "conference call",
        "wins award",
    ),
}


@dataclass(slots=True)
class RunSummary:
    run_id: str
    started_at: str
    completed_at: str = ""
    due_job_count: int = 0
    successful_job_count: int = 0
    failed_job_count: int = 0
    candidate_count: int = 0
    candidate_pool_before_cap: int = 0
    candidate_source_count_before_cap: int = 0
    selected_source_count: int = 0
    selected_source_job_count: int = 0
    new_candidate_count: int = 0
    kept_event_count: int = 0
    evidence_drop_count: int = 0
    needs_primary_source_count: int = 0
    evidence_failure_count: int = 0
    editor_attempted: bool = False
    edition_story_count: int = 0
    delivery_status: str = "NOT_DUE"
    send_id: str = ""
    health_reasons: tuple[str, ...] = ()
    error_codes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DigestOrchestrator:
    """Coordinate deterministic boundaries; models never control writes or recipients."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        repository: GoogleSheetsRepository,
        collector: Collector,
        evidence: EvidenceAnalyst,
        editor: DailyEditor,
        agentmail: AgentMailClient,
        researcher: WebResearchScout | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.collector = collector
        self.evidence = evidence
        self.editor = editor
        self.agentmail = agentmail
        self.researcher = researcher
        self.clock = clock or (lambda: datetime.now(UTC))
        self.last_edition: Edition | None = None
        self.last_rendered: RenderedEmail | None = None

    async def run_once(
        self,
        *,
        dry_run: bool = False,
        collect_only: bool = False,
        force_editor: bool = False,
        send_once: bool = False,
        require_story: bool = False,
        allow_degraded_preview: bool = False,
        full_brief: bool = False,
        send_revision: str | None = None,
        manual_source_id: str | None = None,
    ) -> RunSummary:
        now = self._now()
        edition_date = now.astimezone(self._zone()).date()
        if send_revision and not send_once:
            raise ValueError("send_revision is allowed only with send_once")

        sources = self.repository.load_sources(active_only=True)
        sheet_settings = self.repository.load_settings()
        effective_test = self._effective_test_mode(sheet_settings)
        run_id = self._run_id(now, dry_run=dry_run, test_mode=effective_test)
        summary = RunSummary(run_id=run_id, started_at=now.isoformat())
        send_id = _edition_send_id(edition_date, send_revision)
        if send_once:
            summary.send_id = send_id
            if not dry_run and self.repository.digest_was_sent(send_id):
                summary.delivery_status = "ALREADY_SENT"
                summary.completed_at = self._now().isoformat()
                return summary
        company_watchlist = self.repository.load_company_watchlist()
        brand_domains = build_brand_domains(sources, company_watchlist)
        deposit_watchlist = self.repository.load_deposit_watch()
        health_prefixes = ("TEST-",) if effective_test else ("RUN-",)
        prior_health = self.repository.load_source_health(run_prefixes=health_prefixes)
        # Evidence-model failures are useful diagnostics, but they must not replace the
        # collector receipt used for source cadence and coverage decisions.
        collection_health = [
            receipt
            for receipt in prior_health
            if receipt.failure_stage != "evidence_guard"
            and receipt.run_id.startswith("TEST-" if effective_test else "RUN-")
        ]
        prior_by_job = latest_health_by_job(collection_health)
        recipients = self.repository.load_recipients(
            exclude_addresses=(self.settings.agentmail_inbox,), max_recipients=49
        )
        forbidden_addresses = tuple(recipient.email for recipient in recipients.recipients)

        all_jobs = expand_source_jobs(sources, company_watchlist, manual=False)
        fresh_full_sweep = (
            send_once or self._inside_send_window(now) or (force_editor and full_brief)
        )
        if manual_source_id is None and fresh_full_sweep:
            # Delivery mornings, an explicit send-once, and an explicitly requested full
            # preview are always a fresh full sweep. A recent preview or ad-hoc production
            # run must not suppress a source that published minutes later.
            # Active quarantines remain respected so a known-bad endpoint is not
            # hammered simply because a delivery is due.
            due_jobs = [
                job
                for job in all_jobs
                if not (
                    (prior := prior_by_job.get(job.source_job_key))
                    and prior.quarantined_until is not None
                    and prior.quarantined_until > now
                )
            ]
        else:
            due_jobs = select_due_source_jobs(
                sources,
                company_watchlist,
                collection_health,
                now=now,
                manual=manual_source_id is not None,
                manual_source_id=manual_source_id,
            )
        summary.due_job_count = len(due_jobs)
        collection_results = await self.collector.collect(due_jobs, run_id=run_id)
        editor_requested = force_editor or send_once or self._inside_send_window(now)
        known_candidate_ids, known_hashes = self.repository.load_candidate_index(
            include_test_only=effective_test,
            # A prior NEEDS_PRIMARY_SOURCE outcome is a hold, not a permanent rejection.
            # Retry it on the three editorial mornings (and explicit previews/sends),
            # while durable DROP and published-event receipts still deduplicate normally.
            include_needs_primary=not editor_requested,
            # Legacy evidence decisions were made under a deal-only policy. Reconsider
            # them once under the broader intelligence policy without disturbing any
            # already-published raw event or its durable deduplication receipt.
            evidence_policy_version=EVIDENCE_POLICY_VERSION,
        )
        last_sent_at = self.repository.load_last_sent_at()

        # Search is a bounded discovery lane, not a publication shortcut. Gemini may
        # find direct source URLs, but those pages are collected, freshness-checked,
        # deduplicated, and evidence-reviewed exactly like registry candidates.
        research_results: tuple[JobCollectionResult, ...] = ()
        if self.researcher is not None and editor_requested and not collect_only:
            research_start = _analysis_window_start(
                now,
                last_sent_at,
                editor_requested=editor_requested,
                full_brief=full_brief,
            )
            try:
                research_seeds = _research_seed_headlines(
                    flatten_candidates(collection_results),
                    limit=16,
                )
                research_jobs = await self.researcher.discover_jobs(
                    start=research_start,
                    end=now,
                    seed_headlines=[candidate.title for candidate in research_seeds],
                )
                if research_jobs:
                    research_results = await self.collector.collect(
                        research_jobs,
                        run_id=run_id,
                    )
            except Exception as error:  # supplemental research must not erase core coverage
                summary.error_codes.append(
                    _reason_code(f"WEB_RESEARCH_{type(error).__name__}_{str(error)[:120]}")
                )

        new_candidates: list[CandidateV1] = []
        current_receipts: list[SourceHealth] = []
        by_job = {result.source_job_key: result for result in collection_results}
        for job in due_jobs:
            result = by_job[job.source_job_key]
            if result.succeeded:
                summary.successful_job_count += 1
                fresh = []
                for candidate in result.candidates:
                    if (
                        candidate.candidate_id in known_candidate_ids
                        or candidate.content_sha256 in known_hashes
                    ):
                        continue
                    fresh.append(candidate)
                    known_candidate_ids.add(candidate.candidate_id)
                    known_hashes.add(candidate.content_sha256)
                new_candidates.extend(fresh)
            else:
                summary.failed_job_count += 1
                summary.error_codes.append(_reason_code(result.error_type or "COLLECTION_FAILED"))
                fresh = []
            receipt = _source_receipt(
                job,
                result,
                new_candidate_count=len(fresh),
                previous=prior_by_job.get(job.source_job_key),
                run_id=run_id,
                now=now,
                quarantine_after=int(sheet_settings.get("source_quarantine_after_failures", 3)),
                quarantine_hours=int(sheet_settings.get("source_quarantine_hours", 24)),
            )
            current_receipts.append(receipt)
            prior_by_job[job.source_job_key] = receipt

        for result in research_results:
            if not result.succeeded:
                summary.error_codes.append(
                    _reason_code(f"WEB_RESEARCH_{result.error_type or 'COLLECTION_FAILED'}")
                )
                continue
            for candidate in result.candidates:
                if (
                    candidate.candidate_id in known_candidate_ids
                    or candidate.content_sha256 in known_hashes
                ):
                    continue
                new_candidates.append(candidate)
                known_candidate_ids.add(candidate.candidate_id)
                known_hashes.add(candidate.content_sha256)
        if not dry_run:
            self.repository.append_source_health_many(current_receipts)

        summary.candidate_count = len(flatten_candidates((*collection_results, *research_results)))
        job_item_counts = {
            result.source_job_key: len(result.candidates) for result in collection_results
        }
        job_item_counts.update(
            {result.source_job_key: len(result.candidates) for result in research_results}
        )
        new_candidates = [
            _attach_watchlist_hits(candidate, company_watchlist, deposit_watchlist)
            for candidate in new_candidates
        ]
        new_candidates = [
            candidate for candidate in new_candidates if _candidate_is_relevant(candidate)
        ]
        freshness_cutoff = _analysis_window_start(
            now,
            last_sent_at,
            editor_requested=editor_requested,
            full_brief=full_brief,
        )
        new_candidates = [
            candidate
            for candidate in new_candidates
            if _candidate_is_fresh(
                candidate,
                cutoff=freshness_cutoff,
                now=now,
                source_job_item_count=job_item_counts.get(candidate.source_job_key, 1),
            )
        ]
        summary.candidate_pool_before_cap = len(new_candidates)
        summary.candidate_source_count_before_cap = len(
            {candidate.source_id for candidate in new_candidates}
        )
        new_candidates = _prioritize_candidates(
            new_candidates,
            limit=min(MAX_EVIDENCE_CALLS, self.settings.max_candidates_per_run),
        )
        summary.new_candidate_count = len(new_candidates)
        summary.selected_source_count = len({candidate.source_id for candidate in new_candidates})
        summary.selected_source_job_count = len(
            {candidate.source_job_key for candidate in new_candidates}
        )

        kept_events: list[EventV1] = []
        current_event_rows: list[dict[str, Any]] = []
        raw_event_writes: list[tuple[EventV1, CandidateV1]] = []
        evidence_outcome_receipts: list[SourceHealth] = []
        evidence_semaphore = asyncio.Semaphore(EVIDENCE_CONCURRENCY)

        async def review_candidate(
            candidate: CandidateV1,
        ) -> tuple[CandidateV1, Mapping[str, Any] | None, EventV1 | None, Exception | None]:
            async with evidence_semaphore:
                try:
                    analysis = await self.evidence.analyze(
                        candidate, forbidden_prompt_values=forbidden_addresses
                    )
                    event = event_from_analysis(candidate, analysis, analyzed_at=self._now())
                except Exception as error:  # per-candidate failure isolation remains exact
                    return candidate, None, None, error
                return candidate, analysis, event, None

        evidence_outcomes = await asyncio.gather(
            *(review_candidate(candidate) for candidate in new_candidates)
        )
        for candidate, analysis, event, error in evidence_outcomes:
            if error is not None:
                summary.evidence_failure_count += 1
                summary.error_codes.extend(_safe_error_codes(error))
                failure = _candidate_failure_receipt(
                    candidate,
                    previous=prior_by_job.get(candidate.source_job_key),
                    run_id=run_id,
                    now=self._now(),
                    error_type=type(error).__name__,
                )
                evidence_outcome_receipts.append(failure)
                continue
            if analysis is None:
                summary.error_codes.append("EMPTY_EVIDENCE_ANALYSIS")
                continue
            if event is None:
                decision = str(analysis.get("decision") or "DROP").upper()
                if decision == "NEEDS_PRIMARY_SOURCE":
                    summary.needs_primary_source_count += 1
                else:
                    summary.evidence_drop_count += 1
                evidence_outcome_receipts.append(
                    _candidate_disposition_receipt(
                        candidate,
                        run_id=run_id,
                        now=self._now(),
                        decision=decision,
                    )
                )
                continue
            if candidate.watchlist_hits:
                deposit_hit = any(hit.startswith("deposit:") for hit in candidate.watchlist_hits)
                event = event.model_copy(
                    update={
                        "editorial_score": min(
                            100,
                            event.editorial_score + (15 if deposit_hit else 5),
                        )
                    }
                )
            kept_events.append(event)
            event_row = event.model_dump(mode="python")
            event_row.update(
                {
                    "source_name": candidate.source_name,
                    "content_hash": candidate.content_sha256,
                    "canonical_url": str(candidate.canonical_url),
                    "published_at": candidate.published_at,
                    "observed_at": event.analyzed_at,
                }
            )
            current_event_rows.append(event_row)
            raw_event_writes.append((event, candidate))
        if not dry_run:
            self.repository.append_source_health_many(evidence_outcome_receipts)
            self.repository.upsert_raw_events(
                raw_event_writes,
                status="TEST_ONLY" if effective_test else "ELIGIBLE",
            )
        summary.kept_event_count = len(kept_events)

        inside_send_window = self._inside_send_window(now)
        if collect_only or not (force_editor or send_once or inside_send_window):
            summary.completed_at = self._now().isoformat()
            return summary

        summary.editor_attempted = True
        gate = evaluate_health_gate(
            all_jobs,
            due_jobs,
            prior_by_job,
            threshold=float(
                sheet_settings.get(
                    "source_health_threshold",
                    sheet_settings.get("p0_source_health_threshold", 0.8),
                )
            ),
            now=now,
            collector_completed_at=max(
                (receipt.checked_at for receipt in current_receipts), default=None
            ),
            collector_freshness=timedelta(
                minutes=int(sheet_settings.get("collector_freshness_minutes", 90))
            ),
        )
        summary.health_reasons = gate.reason_codes
        if not gate.healthy and not (dry_run and allow_degraded_preview):
            summary.delivery_status = "HELD_SOURCE_HEALTH"
            if not dry_run:
                self.repository.upsert_digest_run(
                    _digest_run(
                        run_id=run_id,
                        now=now,
                        status=DigestRunStatus.HELD,
                        send_id=send_id,
                        health=gate,
                        model=self.settings.openrouter_model,
                        error=",".join(gate.reason_codes),
                        digest_date=edition_date,
                        candidate_count=summary.candidate_count,
                    )
                )
            summary.completed_at = self._now().isoformat()
            return summary

        summary.send_id = send_id
        if not dry_run and self.repository.digest_was_sent(send_id):
            summary.delivery_status = "ALREADY_SENT"
            summary.completed_at = self._now().isoformat()
            return summary

        try:
            edition, rendered, resumed_ready = await self._prepare_edition(
                send_id=send_id,
                dry_run=dry_run,
                force_editor=force_editor,
                now=now,
                edition_date=edition_date,
                sheet_settings=sheet_settings,
                forbidden_addresses=forbidden_addresses,
                current_event_rows=current_event_rows,
                run_id=run_id,
                full_brief=full_brief,
                send_revision=send_revision,
                brand_domains=brand_domains,
            )
        except Exception as error:
            summary.delivery_status = "FAILED_CLOSED_EDITORIAL"
            summary.error_codes.extend(_safe_error_codes(error))
            if not dry_run:
                self.repository.upsert_digest_run(
                    _digest_run(
                        run_id=run_id,
                        now=now,
                        status=DigestRunStatus.FAILED_CLOSED,
                        send_id=send_id,
                        health=gate,
                        model=self.settings.openrouter_model,
                        error=summary.delivery_status,
                        digest_date=edition_date,
                        candidate_count=summary.candidate_count,
                    )
                )
            summary.completed_at = self._now().isoformat()
            return summary
        self.last_edition = edition
        self.last_rendered = rendered
        summary.edition_story_count = len(edition.stories)

        if require_story and not edition.stories:
            summary.delivery_status = "HELD_NO_MATERIAL_STORIES"
            if not dry_run:
                self.repository.upsert_digest_run(
                    _digest_run(
                        run_id=run_id,
                        now=now,
                        status=DigestRunStatus.HELD,
                        send_id=send_id,
                        health=gate,
                        model=self.settings.openrouter_model,
                        edition=edition,
                        recipients=len(recipients.recipients),
                        error=summary.delivery_status,
                        digest_date=edition_date,
                        candidate_count=summary.candidate_count,
                    )
                )
            summary.completed_at = self._now().isoformat()
            return summary

        if dry_run:
            summary.delivery_status = "DRY_RUN_RENDERED"
            summary.completed_at = self._now().isoformat()
            return summary

        sheet_send_enabled = bool(sheet_settings.get("agentmail_send_enabled", False))
        if force_editor and not send_once:
            summary.delivery_status = "HELD_FORCED_PREVIEW"
        elif effective_test:
            summary.delivery_status = "HELD_TEST_MODE"
        elif not self.settings.agentmail_send_enabled:
            summary.delivery_status = "HELD_ENV_SEND_DISABLED"
        elif not sheet_send_enabled:
            summary.delivery_status = "HELD_SHEET_SEND_DISABLED"
        elif not recipients.recipients:
            summary.delivery_status = "HELD_NO_RECIPIENTS"
        elif recipients.invalid_count or recipients.overflow_count:
            summary.delivery_status = "HELD_RECIPIENT_ERRORS"
        else:
            summary.delivery_status = ""
        if summary.delivery_status:
            self.repository.upsert_digest_run(
                _digest_run(
                    run_id=run_id,
                    now=now,
                    status=DigestRunStatus.HELD,
                    send_id=send_id,
                    health=gate,
                    model=self.settings.openrouter_model,
                    edition=edition,
                    recipients=len(recipients.recipients),
                    error=summary.delivery_status,
                    digest_date=edition_date,
                    candidate_count=summary.candidate_count,
                )
            )
            summary.completed_at = self._now().isoformat()
            return summary

        # Final gate: external Sheet state may have changed while the model was editing.
        final_now = self._now()
        final_sheet_settings = self.repository.load_settings()
        final_effective_test = self._effective_test_mode(final_sheet_settings)
        final_recipients = self.repository.load_recipients(
            exclude_addresses=(self.settings.agentmail_inbox,), max_recipients=49
        )
        final_health_rows = [
            receipt
            for receipt in self.repository.load_source_health(
                run_prefixes=("TEST-",) if final_effective_test else ("RUN-",)
            )
            if receipt.failure_stage != "evidence_guard"
            and receipt.run_id.startswith("TEST-" if final_effective_test else "RUN-")
        ]
        final_gate = evaluate_health_gate(
            all_jobs,
            due_jobs,
            latest_health_by_job(final_health_rows),
            threshold=float(
                final_sheet_settings.get(
                    "source_health_threshold",
                    final_sheet_settings.get("p0_source_health_threshold", 0.8),
                )
            ),
            now=final_now,
            collector_completed_at=max(
                (receipt.checked_at for receipt in current_receipts), default=None
            ),
            collector_freshness=timedelta(
                minutes=int(final_sheet_settings.get("collector_freshness_minutes", 90))
            ),
        )
        if self.repository.digest_was_sent(send_id):
            summary.delivery_status = "ALREADY_SENT"
            summary.completed_at = final_now.isoformat()
            return summary
        if not send_once and not self._inside_send_window(final_now):
            summary.delivery_status = "HELD_SEND_WINDOW_EXPIRED"
        elif not final_gate.healthy:
            summary.delivery_status = "HELD_SOURCE_HEALTH_CHANGED"
        elif final_effective_test:
            summary.delivery_status = "HELD_TEST_MODE_CHANGED"
        elif not bool(final_sheet_settings.get("agentmail_send_enabled", False)):
            summary.delivery_status = "HELD_SHEET_SEND_DISABLED"
        elif not final_recipients.recipients:
            summary.delivery_status = "HELD_NO_RECIPIENTS"
        elif final_recipients.invalid_count or final_recipients.overflow_count:
            summary.delivery_status = "HELD_RECIPIENT_ERRORS"
        else:
            summary.delivery_status = ""
        if summary.delivery_status:
            self.repository.upsert_digest_run(
                _digest_run(
                    run_id=run_id,
                    now=final_now,
                    status=DigestRunStatus.HELD,
                    send_id=send_id,
                    health=final_gate,
                    model=self.settings.openrouter_model,
                    edition=edition,
                    recipients=len(final_recipients.recipients),
                    error=summary.delivery_status,
                    digest_date=edition_date,
                    candidate_count=summary.candidate_count,
                )
            )
            summary.completed_at = final_now.isoformat()
            return summary

        gate = final_gate
        recipients = final_recipients
        included_at = self._now()
        ready_run = _digest_run(
            run_id=run_id,
            now=now,
            status=DigestRunStatus.READY_TO_SEND,
            send_id=send_id,
            health=gate,
            model=self.settings.openrouter_model,
            edition=edition,
            recipients=len(recipients.recipients),
            digest_date=edition_date,
            candidate_count=summary.candidate_count,
        )
        if not resumed_ready:
            self.repository.upsert_digest_run(ready_run)

        try:
            delivery = await self.agentmail.deliver(
                rendered,
                recipients=[recipient.email for recipient in recipients.recipients],
                send_id=send_id,
            )
            self._record_sent(
                edition,
                delivery,
                included_at=included_at,
                gate=gate,
                now=now,
                candidate_count=summary.candidate_count,
            )
        except Exception as error:
            summary.delivery_status = "FAILED_CLOSED_AGENTMAIL"
            summary.error_codes.append(_reason_code(type(error).__name__))
            self.repository.upsert_digest_run(
                _digest_run(
                    run_id=run_id,
                    now=now,
                    status=DigestRunStatus.FAILED_CLOSED,
                    send_id=send_id,
                    health=gate,
                    model=self.settings.openrouter_model,
                    edition=edition,
                    recipients=len(recipients.recipients),
                    error=summary.delivery_status,
                    digest_date=edition_date,
                    candidate_count=summary.candidate_count,
                )
            )
            summary.completed_at = self._now().isoformat()
            return summary

        summary.delivery_status = delivery.status
        summary.completed_at = self._now().isoformat()
        return summary

    async def _prepare_edition(
        self,
        *,
        send_id: str,
        dry_run: bool,
        force_editor: bool,
        now: datetime,
        edition_date: date,
        sheet_settings: Mapping[str, Any],
        forbidden_addresses: Sequence[str],
        current_event_rows: Sequence[Mapping[str, Any]],
        run_id: str,
        full_brief: bool,
        send_revision: str | None,
        brand_domains: Mapping[str, str],
    ) -> tuple[Edition, RenderedEmail, bool]:
        ready_edition = (
            self.repository.load_ready_edition(send_id)
            if not dry_run and not force_editor
            else None
        )
        if ready_edition is not None:
            return (
                ready_edition,
                RenderedEmail(
                    subject=ready_edition.subject,
                    html=ready_edition.html,
                    text=ready_edition.text,
                ),
                True,
            )

        # Editorial runs always recover the complete bounded window. Published-story
        # history, rather than the last successful send timestamp, prevents repeats.
        # This keeps an unpublished Wednesday item eligible for Friday or Monday.
        event_cutoff = now - MAX_LOOKBACK
        eligible = self.repository.load_eligible_events(since=event_cutoff)
        history = self.repository.load_published_history(
            since=now - timedelta(days=int(sheet_settings.get("dedupe_window_days", 7)))
        )
        ranked = rank_eligible_events([*eligible, *current_event_rows], history, now=now, limit=30)
        if ranked:
            draft = await self.editor.draft(
                ranked,
                edition_date=edition_date.isoformat(),
                forbidden_prompt_values=forbidden_addresses,
            )
        else:
            draft = _quiet_day_draft(edition_date.isoformat())
        edition = edition_from_draft(draft, ranked, run_id=run_id, generated_at=self._now())
        rendered = render_digest(
            draft,
            test_mode=self._effective_test_mode(sheet_settings),
            brand_domains=brand_domains,
            subject_prefix="[FULL UPDATE] " if send_revision else "",
        )
        edition = edition.model_copy(
            update={"subject": rendered.subject, "html": rendered.html, "text": rendered.text}
        )
        return edition, rendered, False

    def _record_sent(
        self,
        edition: Edition,
        delivery: DeliveryReceipt,
        *,
        included_at: datetime,
        gate: HealthGateResult,
        now: datetime,
        candidate_count: int,
    ) -> None:
        if not delivery.sent or not delivery.message_id or not delivery.thread_id:
            raise RuntimeError("AgentMail did not return an accepted delivery receipt")
        self.repository.upsert_published_stories(
            edition.stories,
            edition,
            send_id=delivery.send_id,
            recipient_group="sheet_recipients",
            status=DigestRunStatus.SENT,
            included_at=included_at,
        )
        sent_run = _digest_run(
            run_id=edition.run_id,
            now=now,
            status=DigestRunStatus.SENT,
            send_id=delivery.send_id,
            health=gate,
            model=self.settings.openrouter_model,
            edition=edition,
            recipients=delivery.recipient_count,
            digest_date=edition.edition_date,
            candidate_count=candidate_count,
        ).model_copy(
            update={
                "message_id": delivery.message_id,
                "thread_id": delivery.thread_id,
                "accepted_at": datetime.fromisoformat(delivery.accepted_at)
                if delivery.accepted_at
                else self._now(),
                "completed_at": self._now(),
            }
        )
        self.repository.upsert_digest_run(sent_run)

    def _inside_send_window(self, now: datetime) -> bool:
        local = now.astimezone(self._zone())
        return (
            local.weekday() in SCHEDULED_SEND_WEEKDAYS
            and local.hour == self.settings.send_hour_local
            and local.minute < self.settings.send_minute_window
        )

    def _effective_test_mode(self, sheet_settings: Mapping[str, Any]) -> bool:
        return self.settings.test_mode or bool(sheet_settings.get("test_mode", True))

    def _zone(self) -> ZoneInfo:
        return ZoneInfo(self.settings.timezone)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("orchestrator clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _run_id(now: datetime, *, dry_run: bool, test_mode: bool) -> str:
        prefix = "DRY" if dry_run else "TEST" if test_mode else "RUN"
        return f"{prefix}-VDAI-{now.strftime('%Y%m%dT%H%M%SZ')}"


def _source_receipt(
    job: SourceJob,
    result: JobCollectionResult,
    *,
    new_candidate_count: int,
    previous: SourceHealth | None,
    run_id: str,
    now: datetime,
    quarantine_after: int,
    quarantine_hours: int,
) -> SourceHealth:
    prior_failures = previous.consecutive_failures if previous else 0
    if result.succeeded:
        consecutive_failures = 0
        if not result.candidates:
            status = HealthStatus.VALID_EMPTY
        elif new_candidate_count:
            status = HealthStatus.SUCCESS
        else:
            status = HealthStatus.UNCHANGED
        last_success = now
        quarantined_until = None
        failure_stage = "source_complete"
        last_error = ""
        reason_codes: tuple[str, ...] = ()
    else:
        consecutive_failures = prior_failures + 1
        status = (
            HealthStatus.QUARANTINED
            if consecutive_failures >= max(1, quarantine_after)
            else HealthStatus.FAILED
        )
        last_success = previous.last_success_at if previous else None
        quarantined_until = (
            now + timedelta(hours=max(1, quarantine_hours))
            if status is HealthStatus.QUARANTINED
            else None
        )
        failure_stage = "fetch_guard"
        last_error = (result.error_detail or result.error_type or "collection failed")[:1_000]
        reason_codes = (_reason_code(result.error_type or "COLLECTION_FAILED"),)

    first = result.candidates[0] if result.candidates else None
    fetch_meta = first.fetch_meta if first else {}
    return SourceHealth(
        source_id=job.source_id,
        source_job_key=job.source_job_key,
        last_attempt_at=now,
        last_success_at=last_success,
        status=status,
        http_status=_optional_int(fetch_meta.get("http_status")),
        item_count=len(result.candidates),
        etag=str(fetch_meta.get("etag", "")),
        last_modified=str(fetch_meta.get("last_modified", "")),
        consecutive_failures=consecutive_failures,
        quarantined_until=quarantined_until,
        parser_version=job.source.parser_version,
        last_error=last_error,
        updated_at=now,
        run_id=run_id,
        source_name=job.source.source_name,
        checked_at=now,
        failure_stage=failure_stage,
        reason_codes=reason_codes,
        canonical_url=first.canonical_url if first else None,
        parser_lane=job.parser_lane,
        notes="",
    )


def _candidate_failure_receipt(
    candidate: CandidateV1,
    *,
    previous: SourceHealth | None,
    run_id: str,
    now: datetime,
    error_type: str,
) -> SourceHealth:
    return SourceHealth(
        source_id=candidate.source_id,
        source_job_key=candidate.source_job_key,
        last_attempt_at=now,
        last_success_at=previous.last_success_at if previous else None,
        status=HealthStatus.FAILED,
        item_count=1,
        consecutive_failures=(previous.consecutive_failures if previous else 0) + 1,
        parser_version=str(candidate.parse_meta.get("parser_version", "candidate.v1")),
        last_error=_reason_code(error_type),
        updated_at=now,
        run_id=run_id,
        source_name=candidate.source_name,
        checked_at=now,
        failure_stage="evidence_guard",
        reason_codes=(_reason_code(error_type),),
        candidate_id=candidate.candidate_id,
        canonical_url=candidate.canonical_url,
        parser_lane=candidate.parser_lane,
    )


def _candidate_disposition_receipt(
    candidate: CandidateV1,
    *,
    run_id: str,
    now: datetime,
    decision: str,
) -> SourceHealth:
    normalized_decision = _reason_code(decision)
    reason = (
        "NEEDS_PRIMARY_SOURCE" if normalized_decision == "NEEDS_PRIMARY_SOURCE" else "EVIDENCE_DROP"
    )
    return SourceHealth(
        source_id=candidate.source_id,
        source_job_key=candidate.source_job_key,
        last_attempt_at=now,
        last_success_at=now,
        status=HealthStatus.VALID_EMPTY,
        item_count=1,
        consecutive_failures=0,
        parser_version=str(candidate.parse_meta.get("parser_version", "candidate.v1")),
        updated_at=now,
        run_id=run_id,
        source_name=candidate.source_name,
        checked_at=now,
        failure_stage="evidence_guard",
        reason_codes=(reason,),
        candidate_id=candidate.candidate_id,
        canonical_url=candidate.canonical_url,
        parser_lane=candidate.parser_lane,
        notes=json.dumps(
            {
                "content_hash": candidate.content_sha256,
                "disposition": normalized_decision,
                "evidence_policy_version": EVIDENCE_POLICY_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _digest_run(
    *,
    run_id: str,
    now: datetime,
    status: DigestRunStatus,
    send_id: str,
    health: HealthGateResult,
    model: str,
    edition: Edition | None = None,
    recipients: int = 0,
    error: str = "",
    digest_date: date | None = None,
    candidate_count: int = 0,
) -> DigestRun:
    section_counts: dict[str, int] = {}
    if edition:
        for story in edition.stories:
            section_counts[story.section.value] = section_counts.get(story.section.value, 0) + 1
    return DigestRun(
        run_id=run_id,
        digest_date=digest_date or now.date(),
        started_at=now,
        completed_at=now if status is not DigestRunStatus.READY_TO_SEND else None,
        health_status="HEALTHY" if health.healthy else "DEGRADED",
        candidate_count=candidate_count,
        selected_count=len(edition.stories) if edition else 0,
        section_counts=section_counts,
        agent_model=model,
        subject=edition.subject if edition else "",
        send_id=send_id,
        recipient_group=f"sheet_recipients:{recipients}",
        status=status,
        error=error[:2_000],
        edition_json=edition.model_dump(mode="json") if edition else {},
    )


def _attach_watchlist_hits(
    candidate: CandidateV1,
    company_watchlist: Sequence[Mapping[str, Any]],
    deposit_watchlist: Sequence[Mapping[str, Any]],
) -> CandidateV1:
    text = " ".join((candidate.title, candidate.summary_text, candidate.body_text))
    hits = list(candidate.watchlist_hits)
    for prefix, rows in (
        ("company", company_watchlist),
        ("deposit", deposit_watchlist),
    ):
        for row in rows:
            label = next(
                (
                    str(row.get(key) or "").strip()
                    for key in (
                        "company",
                        "company_name",
                        "name",
                        "entity",
                        "organization",
                        "competitor",
                    )
                    if str(row.get(key) or "").strip()
                ),
                "",
            )
            if not label:
                continue
            aliases = [label]
            for key in ("aliases", "alias", "domains", "domain"):
                aliases.extend(normalize_list(row.get(key)))
            if any(_contains_watch_term(text, alias) for alias in aliases):
                safe_label = re.sub(r"[\x00-\x1f\x7f]+", " ", label).strip()[:80]
                hit = f"{prefix}:{safe_label}"
                if safe_label and hit not in hits:
                    hits.append(hit)
            if len(hits) >= 20:
                break
    return candidate.model_copy(update={"watchlist_hits": tuple(hits[:20])})


def _contains_watch_term(text: str, term: str) -> bool:
    normalized_text = " " + re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip() + " "
    normalized_term = re.sub(r"[^a-z0-9]+", " ", str(term).casefold()).strip()
    return len(normalized_term) >= 3 and f" {normalized_term} " in normalized_text


def _candidate_is_relevant(candidate: CandidateV1) -> bool:
    """Bound broad discovery feeds while leaving primary-source judgment to Gemini."""

    normalized_text = (
        " "
        + re.sub(
            r"[^a-z0-9]+",
            " ",
            " ".join(
                (
                    candidate.title or "",
                    candidate.summary_text or "",
                )
            ).casefold(),
        ).strip()
        + " "
    )
    exclusions = _SOURCE_RELEVANCE_EXCLUSIONS.get(candidate.source_id, ())
    if any(
        (normalized := re.sub(r"[^a-z0-9]+", " ", term.casefold()).strip())
        and f" {normalized} " in normalized_text
        for term in exclusions
    ):
        return False

    if candidate.source_id not in _STRICT_SOURCE_RELEVANCE_IDS:
        return True
    rules = _SOURCE_RELEVANCE_RULES.get(candidate.source_id, ())
    return bool(rules) and all(
        any(_contains_watch_term(normalized_text, term) for term in group) for group in rules
    )


def _prioritize_candidates(candidates: Sequence[CandidateV1], *, limit: int) -> list[CandidateV1]:
    """Choose diverse items while reserving bounded review space for web discoveries."""

    priority = {"P0": 0, "P1": 1, "P2": 2}
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            not bool(candidate.watchlist_hits),
            not candidate.primary_source,
            priority.get(candidate.priority.value, 9),
            -_candidate_priority_signal(candidate),
            -(candidate.published_at.timestamp() if candidate.published_at else 0),
            candidate.candidate_id,
        ),
    )
    selected: list[CandidateV1] = []
    job_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    ceiling = min(limit, MAX_EVIDENCE_CALLS)

    def add(candidate: CandidateV1, *, allow_repeat_source: bool = False) -> bool:
        canonical_url = str(candidate.canonical_url)
        if job_counts.get(candidate.source_job_key, 0) >= 2:
            return False
        if source_counts.get(candidate.source_id, 0) >= 2:
            return False
        if not allow_repeat_source and source_counts.get(candidate.source_id, 0) >= 1:
            return False
        if canonical_url in seen_urls or candidate.content_sha256 in seen_hashes:
            return False
        job_counts[candidate.source_job_key] = job_counts.get(candidate.source_job_key, 0) + 1
        source_counts[candidate.source_id] = source_counts.get(candidate.source_id, 0) + 1
        seen_urls.add(canonical_url)
        seen_hashes.add(candidate.content_sha256)
        selected.append(candidate)
        return True

    def fill_balanced(
        pool: Sequence[CandidateV1],
        *,
        target: int,
        allow_repeat_source: bool = False,
    ) -> None:
        lanes = ("venture_debt", "competitor_deposit", "ai")
        # Each source receives a first review opportunity before any source receives
        # a second. Lane rotation then prevents a large AI or press-release feed from
        # crowding out venture debt, bank/deposit, or fintech sources.
        by_lane_source: dict[str, dict[str, list[CandidateV1]]] = {
            lane: {} for lane in lanes
        }
        for candidate in pool:
            source_bucket = by_lane_source[_candidate_lane(candidate)].setdefault(
                candidate.source_id, []
            )
            source_bucket.append(candidate)

        max_depth = max(
            (
                len(source_candidates)
                for lane_sources in by_lane_source.values()
                for source_candidates in lane_sources.values()
            ),
            default=0,
        )
        for depth in range(max_depth):
            while len(selected) < target:
                progressed = False
                for lane in lanes:
                    for source_candidates in by_lane_source[lane].values():
                        if depth >= len(source_candidates):
                            continue
                        if add(
                            source_candidates[depth],
                            allow_repeat_source=allow_repeat_source,
                        ):
                            progressed = True
                            break
                    if len(selected) >= target:
                        break
                if not progressed:
                    break

    web_candidates = [candidate for candidate in ordered if _is_web_research_candidate(candidate)]
    registry_candidates = [
        candidate for candidate in ordered if not _is_web_research_candidate(candidate)
    ]
    web_reserve = min(WEB_RESEARCH_EVIDENCE_RESERVE, len(web_candidates), ceiling)
    debt_secondary_candidates = [
        candidate
        for candidate in registry_candidates
        if candidate.section_hint is DigestSection.DEAL_TAPE
        and not candidate.primary_source
        and not candidate.discovery_only
        and candidate.source_class in {SourceTier.B, SourceTier.C}
    ]
    debt_secondary_ids = {candidate.candidate_id for candidate in debt_secondary_candidates}
    general_registry_candidates = [
        candidate
        for candidate in registry_candidates
        if candidate.candidate_id not in debt_secondary_ids
    ]
    debt_secondary_reserve = min(
        DEBT_SECONDARY_EVIDENCE_RESERVE,
        len(debt_secondary_candidates),
        max(0, ceiling - web_reserve),
    )

    # Primary registry feeds preserve breadth, while current secondary debt reporting
    # and Gemini's grounded discoveries each get a real chance to be evaluated instead
    # of being crowded out by hundreds of first-party AI feed items.
    fill_balanced(
        general_registry_candidates,
        target=ceiling - web_reserve - debt_secondary_reserve,
    )
    fill_balanced(
        debt_secondary_candidates,
        target=min(ceiling - web_reserve, len(selected) + debt_secondary_reserve),
    )
    fill_balanced(web_candidates, target=min(ceiling, len(selected) + web_reserve))
    # The reserve lanes may finish below their targets when several discoveries
    # share one synthetic source. Complete the budget with the same source-first
    # rotation, never by returning to the globally highest-ranked source twice.
    fill_balanced(ordered, target=ceiling)
    # Only after every distinct candidate-producing source has had a chance may
    # especially rich sources contribute a second item.
    fill_balanced(ordered, target=ceiling, allow_repeat_source=True)
    return selected


def _candidate_priority_signal(candidate: CandidateV1) -> int:
    """Favor concrete financing announcements inside a lane without judging evidence."""

    if candidate.section_hint is not DigestSection.DEAL_TAPE:
        return 0
    normalized = " ".join(
        (str(getattr(candidate, "title", "")), str(getattr(candidate, "summary_text", "")))
    ).casefold()
    signal = 0
    for phrase, weight in (
        ("venture debt", 8),
        ("growth debt", 8),
        ("senior secured", 7),
        ("credit facility", 7),
        ("financing facility", 7),
        ("term loan", 6),
        ("debt financing", 6),
        ("asset-backed", 5),
        ("asset backed", 5),
        ("refinancing", 4),
    ):
        if phrase in normalized:
            signal += weight
    if re.search(
        r"\b[$€£]\s?\d|\b\d+(?:\.\d+)?\s?(?:m|mm|bn|million|billion)\b",
        normalized,
        re.IGNORECASE,
    ):
        signal += 3
    return signal


def _research_seed_headlines(
    candidates: Sequence[CandidateV1], *, limit: int
) -> list[CandidateV1]:
    """Select concrete discovery headlines for Gemini to resolve to direct sources."""

    transaction_terms = {
        "venture debt": 7,
        "growth debt": 7,
        "financing facility": 6,
        "senior secured": 5,
        "credit facility": 5,
        "debt financing": 5,
        "loan facility": 5,
        "debt deal": 4,
        "private credit": 3,
        "term loan": 3,
        "asset backed": 3,
        "asset-backed": 3,
        "secures": 2,
        "closes": 2,
        "provides": 2,
        "receives": 2,
    }
    innovation_terms = (
        "startup",
        "technology",
        "software",
        "artificial intelligence",
        " ai ",
        "biotech",
        "medical",
        "life science",
        "aerospace",
        "aircraft",
        "robotaxi",
    )

    def score(candidate: CandidateV1) -> tuple[int, float, str]:
        normalized = " " + re.sub(r"[^a-z0-9]+", " ", candidate.title.casefold()).strip() + " "
        relevance = sum(
            weight for term, weight in transaction_terms.items() if term in normalized
        )
        relevance += 2 * sum(term in normalized for term in innovation_terms)
        amount_pattern = r"\b[$€£]\s?\d|\b\d+(?:\.\d+)?\s?(?:m|mm|bn|million|billion)\b"
        relevance += 1 if re.search(amount_pattern, candidate.title, re.IGNORECASE) else 0
        relevance += 2 if candidate.watchlist_hits else 0
        published = candidate.published_at.timestamp() if candidate.published_at else 0
        return relevance, published, candidate.candidate_id

    discovery = [candidate for candidate in candidates if candidate.discovery_only]
    fallback = [candidate for candidate in candidates if not candidate.discovery_only]
    ordered = sorted(discovery, key=score, reverse=True) + sorted(
        fallback, key=score, reverse=True
    )
    selected: list[CandidateV1] = []
    seen_titles: set[str] = set()
    for candidate in ordered:
        normalized_title = re.sub(r"\s+-\s+[^-]+$", "", candidate.title).casefold().strip()
        normalized_title = re.sub(r"[^a-z0-9]+", " ", normalized_title).strip()
        if not normalized_title or normalized_title in seen_titles:
            continue
        seen_titles.add(normalized_title)
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def _analysis_window_start(
    now: datetime,
    last_sent_at: datetime | None,
    *,
    editor_requested: bool,
    full_brief: bool,
) -> datetime:
    """Return the bounded discovery cutoff for collection or editorial work.

    Every editorial run rechecks the eight-day recovery window. Publication history
    handles deduplication, so a prior send never makes an unpublished item disappear.
    Non-editor collection runs retain the smaller bootstrap/incremental window.
    """

    earliest = now - MAX_LOOKBACK
    if editor_requested or full_brief:
        return earliest
    return max(earliest, last_sent_at or now - INITIAL_LOOKBACK)


def _candidate_is_fresh(
    candidate: CandidateV1,
    *,
    cutoff: datetime,
    now: datetime,
    source_job_item_count: int = 1,
) -> bool:
    """Reject stale source-dated archive items before they consume model calls.

    Some official changelog and product pages do not expose a parseable publish date. A
    single-page snapshot may be reviewed on first observation, but an undated multi-item
    archive cannot safely claim daily freshness. Durable candidate/hash receipts prevent
    repeated review of accepted undated snapshots.
    """

    if candidate.published_at is not None:
        observed = candidate.published_at
    else:
        title = candidate.title.strip()
        if title.casefold().startswith(("<!doctype", "<html", "<head", "<body")):
            return False
        match = _TITLE_DATE.search(title)
        if match is not None:
            try:
                parsed = datetime.fromisoformat(match.group(0)).replace(tzinfo=UTC)
            except ValueError:
                parsed = None
                for pattern in ("%B %d, %Y", "%b %d, %Y"):
                    try:
                        parsed = datetime.strptime(match.group(0), pattern).replace(tzinfo=UTC)
                        break
                    except ValueError:
                        continue
            if parsed is not None:
                observed = parsed
            elif source_job_item_count > 1:
                return False
            else:
                observed = candidate.retrieved_at
        elif source_job_item_count > 1:
            return False
        else:
            observed = candidate.retrieved_at
    return cutoff <= observed <= now + timedelta(hours=1)


def _candidate_lane(candidate: CandidateV1) -> str:
    if candidate.section_hint is DigestSection.AI_RADAR:
        return "ai"
    if candidate.section_hint in {
        DigestSection.COMPETITIVE_FIELD,
        DigestSection.DEPOSIT_WATCH,
    }:
        return "competitor_deposit"
    return "venture_debt"


def _is_web_research_candidate(candidate: CandidateV1) -> bool:
    return candidate.source_id.startswith("WEB-")


def _edition_send_id(edition_date: date, revision: str | None) -> str:
    if not revision:
        audience = "daily"
    else:
        normalized = revision.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._~-]{0,47}", normalized):
            raise ValueError("send_revision must be a safe 1-48 character label")
        audience = f"daily-{normalized}"
    return build_send_id(edition_date.isoformat(), audience)


def _quiet_day_draft(edition_date: str) -> dict[str, Any]:
    return {
        "edition_date": edition_date,
        "edition": {
            "kicker": "VENTURE DEBT + AI",
            "headline": "Quiet tape. Nothing cleared the evidence bar.",
            "deck": (
                "Coverage was healthy; the verified developments were not material enough to send."
            ),
        },
        "lead": None,
        "deal_tape": [],
        "competitive_field": [],
        "ai_radar": [],
        "runway_watch": [],
        "quiet_day": True,
        "validated_stories": [],
        "story_count": 0,
        "word_count": 16,
    }


def _reason_code(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value).upper()).strip("_")[:80] or "UNKNOWN"


def _safe_error_codes(error: Exception) -> list[str]:
    explicit = getattr(error, "codes", ())
    if isinstance(explicit, (list, tuple)) and explicit:
        return [_reason_code(code) for code in explicit[:20]]
    return [_reason_code(type(error).__name__)]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "EVIDENCE_POLICY_VERSION",
    "MAX_EVIDENCE_CALLS",
    "DigestOrchestrator",
    "RunSummary",
]
