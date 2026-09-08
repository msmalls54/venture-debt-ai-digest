from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from vdai.models import (
    CandidateV1,
    Citation,
    DigestSection,
    Edition,
    EventV1,
    EvidenceDecision,
    Fact,
    ParserLane,
    Priority,
    Recipient,
    RecipientBatch,
    SourceTier,
    Story,
)

NOW = datetime(2026, 8, 25, 15, 0, tzinfo=UTC)


def candidate_data() -> dict[str, object]:
    return {
        "run_id": "run-1",
        "candidate_id": "candidate-1",
        "source_id": "S001",
        "source_job_key": "S001",
        "source_name": "Borrower newsroom",
        "section_hint": DigestSection.DEAL_TAPE,
        "source_class": SourceTier.A,
        "parser_lane": ParserLane.RSS_ATOM,
        "priority": Priority.P0,
        "primary_source": True,
        "discovery_only": False,
        "retrieved_at": NOW,
        "item_id": "release-1",
        "raw_url": "https://borrower.example/releases/1",
        "canonical_url": "https://borrower.example/releases/1",
        "title": "Borrower closes debt facility",
        "published_at": NOW,
        "summary_text": "A concise summary.",
        "body_text": "Borrower closed a $25 million facility with Lender.",
        "content_sha256": "a" * 64,
        "dedupe_key": "candidate-1",
    }


def fact() -> Fact:
    return Fact(
        fact_id="fact-1",
        claim="Borrower closed a $25 million facility.",
        exact_quote="closed a $25 million facility",
        source_id="S001",
        source_url="https://borrower.example/releases/1",
        evidence_tier=SourceTier.A,
    )


def test_candidate_v1_is_strict_and_bounded() -> None:
    candidate = CandidateV1.model_validate(candidate_data())
    assert candidate.schema_version == "candidate.v1"
    assert candidate.body_text.startswith("Borrower")
    with pytest.raises(ValidationError):
        CandidateV1.model_validate({**candidate_data(), "unexpected": True})
    with pytest.raises(ValidationError):
        CandidateV1.model_validate({**candidate_data(), "body_text": "x" * 15_001})
    with pytest.raises(ValidationError):
        CandidateV1.model_validate({**candidate_data(), "content_sha256": "not-a-hash"})


def test_keep_event_requires_source_backed_facts_and_keeps_amounts_separate() -> None:
    event = EventV1(
        event_id="event-1",
        candidate_id="candidate-1",
        run_id="run-1",
        source_id="S001",
        source_job_key="S001",
        decision=EvidenceDecision.KEEP,
        section=DigestSection.DEAL_TAPE,
        event_type="venture_debt",
        event_status="closed",
        company="Borrower",
        borrower="Borrower",
        lenders=("Lender",),
        total_commitment=Decimal("25000000"),
        initial_funding=Decimal("10000000"),
        currency="USD",
        evidence_tier=SourceTier.A,
        facts=(fact(),),
        source_ids=("S001",),
        confidence=95,
        editorial_score=82,
        analyzed_at=NOW,
    )
    assert event.total_commitment == Decimal("25000000")
    assert event.initial_funding == Decimal("10000000")
    with pytest.raises(ValidationError, match="source-backed fact"):
        EventV1.model_validate({**event.model_dump(), "facts": ()})


def test_recipient_payload_helper_never_exposes_addresses() -> None:
    batch = RecipientBatch(
        recipients=(Recipient(email="  OWNER@Example.com "),),
        invalid_count=2,
    )
    assert str(batch.recipients[0].email) == "owner@example.com"
    payload = batch.model_payload()
    assert payload == {
        "valid_count": 1,
        "invalid_count": 2,
        "duplicate_count": 0,
        "excluded_count": 0,
        "overflow_count": 0,
    }
    assert "example.com" not in repr(payload)


def test_edition_model_payload_has_no_delivery_audience() -> None:
    story = Story(
        published_story_id="story-1",
        candidate_id="candidate-1",
        raw_event_id="event-1",
        section=DigestSection.DEAL_TAPE,
        rank=1,
        headline="Borrower — $25M facility closed",
        dek="The company added a lender-backed runway extension.",
        why_it_matters="The named lender won a visible mandate.",
        fact_ids=("fact-1",),
        source_ids=("S001",),
        citations=(
            Citation(
                source_id="S001",
                url="https://borrower.example/releases/1",
                fact_ids=("fact-1",),
            ),
        ),
    )
    edition = Edition(
        run_id="run-1",
        edition_date=date(2026, 8, 25),
        subject="Venture Debt + AI — August 25",
        stories=(story,),
        word_count=70,
        generated_at=NOW,
    )
    payload = edition.model_payload()
    assert set(payload) == {"edition_date", "subject", "stories", "word_count"}
    assert "recipient" not in repr(payload).lower()
    assert "email" not in repr(payload).lower()
