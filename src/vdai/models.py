"""Strict data contracts for the venture-debt and AI digest.

The models in this module are the boundary between untrusted source/Sheet data and the
collector, evidence analyst, editor, and delivery layers.  Recipient addresses are
intentionally absent from every model-payload helper.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AnyHttpUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", to_lower=True)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class StrictModel(BaseModel):
    """Shared fail-closed Pydantic configuration."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        validate_default=True,
    )


class DigestSection(StrEnum):
    DISCOVERY = "discovery"
    LEAD = "lead"
    DEAL_TAPE = "deal_tape"
    COMPETITIVE_FIELD = "competitive_field"
    AI_RADAR = "ai_radar"
    RUNWAY_WATCH = "runway_watch"
    DEPOSIT_WATCH = "deposit_watch"


class Priority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class SourceTier(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class ParserLane(StrEnum):
    RSS_ATOM = "rss_atom"
    API = "api"
    HTML = "html"
    SITEMAP_HTML = "sitemap_html"
    MARKDOWN = "markdown"
    HTML_EMAIL = "html_email"


class EvidenceDecision(StrEnum):
    KEEP = "KEEP"
    DROP = "DROP"
    NEEDS_PRIMARY_SOURCE = "NEEDS_PRIMARY_SOURCE"


class EventStatus(StrEnum):
    ANNOUNCED = "announced"
    APPROVED = "approved"
    SIGNED = "signed"
    CLOSED = "closed"
    FUNDED = "funded"
    DRAWN = "drawn"
    LAUNCHED = "launched"
    PROPOSED = "proposed"
    UNKNOWN = "unknown"


class HealthStatus(StrEnum):
    SUCCESS = "SUCCESS"
    UNCHANGED = "UNCHANGED"
    VALID_EMPTY = "VALID_EMPTY"
    QUARANTINED = "QUARANTINED"
    FAILED = "FAILED"


class DigestRunStatus(StrEnum):
    READY_TO_SEND = "READY_TO_SEND"
    SENT = "SENT"
    FAILED_CLOSED = "FAILED_CLOSED"
    HELD = "HELD"


class SourceConfig(StrictModel):
    """One validated row from the Sources tab."""

    source_id: NonEmptyStr
    section: DigestSection
    organization: NonEmptyStr
    source_name: NonEmptyStr
    url: NonEmptyStr
    method: NonEmptyStr
    parser: str = ""
    cadence: NonEmptyStr = "daily"
    source_tier: SourceTier = SourceTier.D
    priority: Priority = Priority.P2
    active: bool = True
    region: tuple[str, ...] = ()
    notes: str = ""
    allowed_domains: tuple[NonEmptyStr, ...]
    primary_source: bool = False
    discovery_only: bool = True
    headers_profile: str = "default"
    max_items: int = Field(default=25, ge=1, le=100)
    max_pages: int = Field(default=3, ge=1, le=10)
    max_bytes: int = Field(default=1_000_000, ge=10_000, le=5_000_000)
    parser_version: NonEmptyStr = "v1"
    logo_domain: str = ""

    @field_validator("url")
    @classmethod
    def https_source_url_or_template(cls, value: str) -> str:
        if not value.lower().startswith("https://"):
            raise ValueError("source URL must use HTTPS")
        if "\\" in value or any(ord(character) < 32 for character in value):
            raise ValueError("source URL contains unsafe characters")
        return value

    @field_validator("allowed_domains")
    @classmethod
    def require_domains(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(domain.lower().strip(".") for domain in value if domain))
        if not normalized:
            raise ValueError("at least one allowed domain is required")
        return normalized

    @field_validator("logo_domain")
    @classmethod
    def logo_domain_is_a_hostname(cls, value: str) -> str:
        normalized = value.strip().lower().strip(".")
        if not normalized:
            return ""
        if (
            "://" in normalized
            or "/" in normalized
            or "@" in normalized
            or not re.fullmatch(r"[a-z0-9.-]+", normalized)
            or "." not in normalized
        ):
            raise ValueError("logo_domain must be a bare hostname")
        return normalized

    @model_validator(mode="after")
    def source_classification_is_coherent(self) -> Self:
        if self.primary_source and self.discovery_only:
            raise ValueError("a source cannot be both primary and discovery-only")
        return self


class SourceJob(StrictModel):
    """One current, optionally watchlist-expanded source collection job."""

    source: SourceConfig
    source_job_key: NonEmptyStr
    resolved_url: AnyHttpUrl | None
    parser_lane: ParserLane
    cadence_minutes: int = Field(ge=15)
    template_values: dict[str, str] = Field(default_factory=dict)
    due: bool = True
    manual_canary: bool = False
    write_mode: Literal["scheduled", "shadow"] = "scheduled"
    configuration_errors: tuple[NonEmptyStr, ...] = ()

    @property
    def ready(self) -> bool:
        return self.resolved_url is not None and not self.configuration_errors

    @property
    def source_id(self) -> str:
        return self.source.source_id

    @property
    def url(self) -> str:
        return str(self.resolved_url) if self.resolved_url is not None else self.source.url


class CandidateV1(StrictModel):
    """Sanitized, deterministic parser output passed to evidence analysis."""

    schema_version: Literal["candidate.v1"] = "candidate.v1"
    run_id: NonEmptyStr
    candidate_id: NonEmptyStr
    source_id: NonEmptyStr
    source_job_key: NonEmptyStr
    source_name: NonEmptyStr
    section_hint: DigestSection
    source_class: SourceTier
    parser_lane: ParserLane
    priority: Priority
    primary_source: bool
    discovery_only: bool
    retrieved_at: AwareDatetime
    item_id: NonEmptyStr
    raw_url: AnyHttpUrl
    canonical_url: AnyHttpUrl
    title: NonEmptyStr
    published_at: AwareDatetime | None = None
    summary_text: str = Field(default="", max_length=5_000)
    body_text: NonEmptyStr = Field(max_length=15_000)
    watchlist_hits: tuple[str, ...] = ()
    content_sha256: Sha256
    dedupe_key: NonEmptyStr
    native_ids: dict[str, str] = Field(default_factory=dict)
    fetch_meta: dict[str, Any] = Field(default_factory=dict)
    parse_meta: dict[str, Any] = Field(default_factory=dict)
    jurisdictions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def canonical_url_is_source_scoped(self) -> Self:
        if self.discovery_only and self.primary_source:
            raise ValueError("candidate source classification is contradictory")
        return self

    def evidence_model_payload(self) -> dict[str, Any]:
        """Return only bounded evidence input; recipient data cannot enter this model."""

        return self.model_dump(mode="json")


Candidate = CandidateV1


class Fact(StrictModel):
    fact_id: NonEmptyStr
    claim: NonEmptyStr
    exact_quote: NonEmptyStr = Field(max_length=1_000)
    source_id: NonEmptyStr
    source_url: AnyHttpUrl
    evidence_tier: SourceTier
    field: str | None = None
    value: str | int | float | bool | None = None


class EventV1(StrictModel):
    """Evidence-verified event. Unknown financial values remain ``None``."""

    schema_version: Literal["event.v1"] = "event.v1"
    event_id: NonEmptyStr
    candidate_id: NonEmptyStr
    run_id: NonEmptyStr
    source_id: NonEmptyStr
    source_job_key: NonEmptyStr
    decision: EvidenceDecision
    section: DigestSection
    event_type: NonEmptyStr
    event_status: EventStatus = EventStatus.UNKNOWN
    company: str | None = None
    borrower: str | None = None
    lenders: tuple[str, ...] = ()
    counterparty: str | None = None
    total_commitment: Decimal | None = Field(default=None, ge=0)
    initial_funding: Decimal | None = Field(default=None, ge=0)
    currency: CurrencyCode | None = None
    facility_type: str | None = None
    maturity: Annotated[str, StringConstraints(max_length=200)] | None = None
    rate: str | None = None
    security: str | None = None
    sector: str | None = None
    geography: tuple[str, ...] = ()
    acquisition_stage: str | None = None
    partner_banks: tuple[str, ...] = ()
    cash_vehicle: str | None = None
    evidence_tier: SourceTier
    facts: tuple[Fact, ...] = ()
    source_ids: tuple[str, ...]
    corroborating_urls: tuple[AnyHttpUrl, ...] = ()
    confidence: int = Field(ge=0, le=100)
    editorial_score: int = Field(ge=0, le=100)
    reasoning: str = Field(default="", max_length=1_500)
    analyzed_at: AwareDatetime

    @model_validator(mode="after")
    def kept_events_require_evidence(self) -> Self:
        if self.decision is EvidenceDecision.KEEP and not self.facts:
            raise ValueError("KEEP events require at least one source-backed fact")
        if self.decision is EvidenceDecision.KEEP and not self.source_ids:
            raise ValueError("KEEP events require at least one source ID")
        return self

    def editorial_model_payload(self) -> dict[str, Any]:
        """Return evidence facts for editing; no delivery audience is represented."""

        return self.model_dump(mode="json")


Event = EventV1


class SourceHealth(StrictModel):
    source_id: NonEmptyStr
    source_job_key: NonEmptyStr
    last_attempt_at: AwareDatetime
    last_success_at: AwareDatetime | None = None
    status: HealthStatus
    http_status: int | None = Field(default=None, ge=100, le=599)
    item_count: int = Field(default=0, ge=0)
    etag: str = ""
    last_modified: str = ""
    consecutive_failures: int = Field(default=0, ge=0)
    quarantined_until: AwareDatetime | None = None
    parser_version: NonEmptyStr
    last_error: str = Field(default="", max_length=2_000)
    updated_at: AwareDatetime
    run_id: NonEmptyStr
    source_name: NonEmptyStr
    checked_at: AwareDatetime
    health_status: HealthStatus | None = None
    failure_stage: Literal[
        "source_complete",
        "registry_guard",
        "source_guard",
        "fetch_guard",
        "candidate_guard",
        "evidence_guard",
    ]
    reason_codes: tuple[str, ...] = ()
    candidate_id: str = ""
    canonical_url: AnyHttpUrl | None = None
    parser_lane: ParserLane | None = None
    notes: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def mirror_health_status(self) -> Self:
        if self.health_status is None:
            self.health_status = self.status
        elif self.health_status is not self.status:
            raise ValueError("status and health_status must match")
        if (
            self.status in {HealthStatus.SUCCESS, HealthStatus.UNCHANGED, HealthStatus.VALID_EMPTY}
            and self.last_success_at is None
        ):
            raise ValueError("healthy source receipt requires last_success_at")
        return self


class Citation(StrictModel):
    source_id: NonEmptyStr
    url: AnyHttpUrl
    publisher: str | None = None
    fact_ids: tuple[str, ...] = ()


class Story(StrictModel):
    published_story_id: NonEmptyStr
    candidate_id: NonEmptyStr
    raw_event_id: NonEmptyStr
    section: DigestSection
    rank: int = Field(ge=1)
    headline: NonEmptyStr = Field(max_length=140)
    dek: NonEmptyStr = Field(max_length=1_200)
    why_it_matters: NonEmptyStr = Field(max_length=5_000)
    company: str = ""
    entity_names: tuple[NonEmptyStr, ...] = ()
    amount: str = ""
    lender: str = ""
    confidence: int = Field(default=0, ge=0, le=100)
    published_at: AwareDatetime | None = None
    fact_ids: tuple[NonEmptyStr, ...]
    source_ids: tuple[NonEmptyStr, ...]
    citations: tuple[Citation, ...]

    @model_validator(mode="after")
    def citations_cover_story_sources(self) -> Self:
        citation_sources = {citation.source_id for citation in self.citations}
        if not set(self.source_ids).issubset(citation_sources):
            raise ValueError("every story source_id requires a citation")
        return self


class Edition(StrictModel):
    run_id: NonEmptyStr
    edition_date: date
    subject: NonEmptyStr = Field(max_length=180)
    stories: tuple[Story, ...] = Field(max_length=10)
    word_count: int = Field(ge=0, le=5_000)
    html: str = ""
    text: str = ""
    generated_at: AwareDatetime

    def model_payload(self) -> dict[str, Any]:
        """Recipient-free editorial payload."""

        return {
            "edition_date": self.edition_date.isoformat(),
            "subject": self.subject,
            "stories": [story.model_dump(mode="json") for story in self.stories],
            "word_count": self.word_count,
        }


class DigestRun(StrictModel):
    run_id: NonEmptyStr
    digest_date: date
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    health_status: Literal["HEALTHY", "DEGRADED", "UNKNOWN"] = "UNKNOWN"
    candidate_count: int = Field(default=0, ge=0)
    selected_count: int = Field(default=0, ge=0, le=10)
    section_counts: dict[str, int] = Field(default_factory=dict)
    agent_model: NonEmptyStr
    subject: str = ""
    send_id: NonEmptyStr
    recipient_group: NonEmptyStr
    status: DigestRunStatus
    message_id: str = ""
    thread_id: str = ""
    accepted_at: AwareDatetime | None = None
    error: str = Field(default="", max_length=2_000)
    execution_url: AnyHttpUrl | None = None
    edition_json: dict[str, Any] = Field(default_factory=dict)


class Recipient(StrictModel):
    recipient_id: str | None = None
    email: EmailStr
    display_name: str | None = None

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> str:
        return str(value).strip().lower()


class RecipientBatch(StrictModel):
    """Validated private-BCC audience plus address-free diagnostics."""

    recipients: tuple[Recipient, ...] = Field(max_length=49)
    invalid_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    excluded_count: int = Field(default=0, ge=0)
    overflow_count: int = Field(default=0, ge=0)

    def model_payload(self) -> dict[str, int]:
        """Expose counts only. Email addresses must never enter an LLM payload."""

        return {
            "valid_count": len(self.recipients),
            "invalid_count": self.invalid_count,
            "duplicate_count": self.duplicate_count,
            "excluded_count": self.excluded_count,
            "overflow_count": self.overflow_count,
        }


__all__ = [
    "Candidate",
    "CandidateV1",
    "Citation",
    "DigestRun",
    "DigestRunStatus",
    "DigestSection",
    "Edition",
    "Event",
    "EventStatus",
    "EventV1",
    "EvidenceDecision",
    "Fact",
    "HealthStatus",
    "ParserLane",
    "Priority",
    "Recipient",
    "RecipientBatch",
    "SourceConfig",
    "SourceHealth",
    "SourceJob",
    "SourceTier",
    "Story",
]
