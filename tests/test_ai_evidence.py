from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest

from vdai.evidence import EvidenceAnalyst, EvidenceRejected, event_from_analysis
from vdai.models import CandidateV1, DigestSection, ParserLane, Priority, SourceTier


class _ScriptedClient:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return deepcopy(self.outputs[len(self.calls) - 1])


def _candidate() -> dict[str, Any]:
    return {
        "candidate_id": "S054:item-1",
        "source_id": "S054",
        "source_name": "OpenAI News",
        "title": "OpenAI launches Model X",
        "summary_text": "The launch adds a lower-cost API tier.",
        "body_text": (
            "OpenAI launched Model X on August 25, 2026. "
            "The company committed $100 million to supporting the new API tier."
        ),
        "canonical_url": "https://openai.com/index/model-x/",
        "published_at": "2026-08-25T08:00:00Z",
        "primary_source": True,
        "discovery_only": False,
        "recipient_email": "must-not-enter-model@example.com",
    }


def _valid_output() -> dict[str, Any]:
    return {
        "candidate_id": "S054:item-1",
        "decision": "KEEP",
        "section": "ai_radar",
        "event_type": "model_launch",
        "event_status": "launched",
        "event_date": "2026-08-25",
        "company": "OpenAI",
        "borrower": "",
        "lenders": [],
        "counterparty": "",
        "amounts": [
            {
                "kind": "other",
                "amount": 100_000_000,
                "currency": "USD",
                "fact_id": "f2",
                "source_text": "$100 million",
            }
        ],
        "facility_type": "",
        "maturity": "",
        "rate": "",
        "security": "",
        "sector": "artificial intelligence",
        "geography": "",
        "acquisition_stage": "",
        "deposit_vehicle": "unknown",
        "partner_banks": [],
        "facts": [
            {
                "fact_id": "f1",
                "claim": "OpenAI launched Model X.",
                "exact_quote": "OpenAI launched Model X on August 25, 2026.",
                "source_url": "https://openai.com/index/model-x/",
            },
            {
                "fact_id": "f2",
                "claim": "OpenAI committed $100 million to support the API tier.",
                "exact_quote": (
                    "The company committed $100 million to supporting the new API tier."
                ),
                "source_url": "https://openai.com/index/model-x/",
            },
        ],
        "confidence": 96,
        "editorial_score": 72,
        "rationale": "The official release directly supports the launch.",
    }


@pytest.mark.asyncio
async def test_evidence_repairs_an_invented_url_once_then_accepts_grounded_output() -> None:
    invalid = _valid_output()
    invalid["facts"][0]["source_url"] = "https://invented.example/story"
    client = _ScriptedClient([invalid, _valid_output()])

    result = await EvidenceAnalyst(client).analyze(  # type: ignore[arg-type]
        _candidate(), forbidden_prompt_values=["must-not-enter-model@example.com"]
    )

    assert result["decision"] == "KEEP"
    assert len(client.calls) == 2
    assert "UNAPPROVED_FACT_URL" in client.calls[1]["messages"][-1]["content"]
    prompt_text = "\n".join(
        message["content"] for call in client.calls for message in call["messages"]
    )
    assert "must-not-enter-model@example.com" not in prompt_text


@pytest.mark.asyncio
async def test_evidence_fails_closed_after_one_repair_for_invented_amount() -> None:
    invalid = _valid_output()
    invalid["amounts"][0]["amount"] = 900_000_000
    client = _ScriptedClient([invalid, invalid])

    with pytest.raises(EvidenceRejected, match="AMOUNT_NOT_SOURCE_DERIVED"):
        await EvidenceAnalyst(client).analyze(_candidate())  # type: ignore[arg-type]

    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_evidence_fails_closed_for_entity_not_present_in_source() -> None:
    invalid = _valid_output()
    invalid["lenders"] = ["Invented Bank"]
    client = _ScriptedClient([invalid, invalid])

    with pytest.raises(EvidenceRejected, match="ENTITY_NOT_IN_SOURCE"):
        await EvidenceAnalyst(client).analyze(_candidate())  # type: ignore[arg-type]

    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_first_party_source_cannot_be_held_for_a_primary_source() -> None:
    needs_primary = _valid_output()
    needs_primary.update(
        {
            "decision": "NEEDS_PRIMARY_SOURCE",
            "section": "none",
            "event_type": "",
            "event_status": "unknown",
            "event_date": "",
            "company": "",
            "amounts": [],
            "facts": [],
            "confidence": 0,
            "editorial_score": 0,
            "rationale": "A better source is needed.",
        }
    )
    client = _ScriptedClient([needs_primary, _valid_output()])

    result = await EvidenceAnalyst(client).analyze(_candidate())  # type: ignore[arg-type]

    assert result["decision"] == "KEEP"
    assert len(client.calls) == 2
    assert (
        "PRIMARY_SOURCE_CANNOT_NEED_PRIMARY"
        in client.calls[1]["messages"][-1]["content"]
    )


@pytest.mark.asyncio
async def test_source_backed_intelligence_drop_uses_one_broad_judgment_pass() -> None:
    candidate = {
        **_candidate(),
        "source_class": "A",
        "section_hint": "ai_radar",
    }
    dropped = _valid_output()
    dropped.update(
        {
            "decision": "DROP",
            "section": "none",
            "event_type": "",
            "event_status": "unknown",
            "event_date": "",
            "company": "",
            "amounts": [],
            "facts": [],
            "confidence": 0,
            "editorial_score": 0,
            "rationale": "Not a completed transaction.",
        }
    )
    client = _ScriptedClient([dropped])

    result = await EvidenceAnalyst(client).analyze(candidate)  # type: ignore[arg-type]

    assert result["decision"] == "DROP"
    assert len(client.calls) == 1
    system_prompt = client.calls[0]["messages"][0]["content"]
    assert "Make that broad intelligence judgment in this single pass" in system_prompt
    assert "final editor, not this evidence pass" in system_prompt


@pytest.mark.asyncio
async def test_evidence_keeps_material_credit_platform_hire_without_deal_parties() -> None:
    candidate = {
        "candidate_id": "S128:item-1",
        "source_id": "S128",
        "source_name": "KKR official press RSS",
        "title": "KKR Expands Global Credit & Markets Platform with Senior Hires",
        "summary_text": (
            "KKR appointed Jonty Edwards and Paula Weisshuber as Managing Directors "
            "in its Credit & Markets business."
        ),
        "body_text": (
            "KKR appointed Jonty Edwards and Paula Weisshuber as Managing Directors "
            "in its Credit & Markets business, strengthening its Credit and Capital "
            "Markets capabilities in Europe."
        ),
        "canonical_url": "https://media.kkr.com/news-details?news_id=official",
        "published_at": "2026-09-02T00:00:00Z",
        "primary_source": True,
        "discovery_only": False,
    }
    output = _valid_output()
    output.update(
        {
            "candidate_id": "S128:item-1",
            "section": "competitive_field",
            "event_type": "credit_platform_expansion",
            "event_status": "announced",
            "event_date": "2026-09-02",
            "company": "KKR",
            "borrower": "",
            "lenders": [],
            "counterparty": "",
            "amounts": [],
            "facility_type": "",
            "sector": "private credit",
            "geography": "Europe",
            "facts": [
                {
                    "fact_id": "f1",
                    "claim": "KKR added two Managing Directors to its Credit & Markets business.",
                    "exact_quote": (
                        "KKR appointed Jonty Edwards and Paula Weisshuber as Managing "
                        "Directors in its Credit & Markets business"
                    ),
                    "source_url": "https://media.kkr.com/news-details?news_id=official",
                }
            ],
            "editorial_score": 84,
            "rationale": "The hires add specific European credit and capital-markets capability.",
        }
    )
    client = _ScriptedClient([output])

    result = await EvidenceAnalyst(client).analyze(candidate)  # type: ignore[arg-type]

    assert result["decision"] == "KEEP"
    assert result["section"] == "competitive_field"
    assert result["borrower"] == ""
    assert result["lenders"] == []
    assert "material finance-platform development" in client.calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_evidence_keeps_well_supported_reported_deal_from_credible_secondary() -> None:
    candidate = {
        "candidate_id": "WEB:playfly-credit",
        "source_id": "WEB-PLAYFLY",
        "source_name": "Web research / ABF Journal",
        "source_class": "C",
        "title": "Bain Capital supports Playfly Sports with $250 million credit facility",
        "summary_text": "Bain Capital provided a $250 million credit facility.",
        "body_text": (
            "Bain Capital provided Playfly Sports with a $250 million credit facility "
            "on September 3, 2026. The financing was announced by the companies."
        ),
        "canonical_url": "https://abfjournal.example/playfly-credit",
        "published_at": "2026-09-03T08:00:00Z",
        "primary_source": False,
        "discovery_only": False,
    }
    output = _valid_output()
    output.update(
        {
            "candidate_id": "WEB:playfly-credit",
            "decision": "KEEP",
            "section": "deal_tape",
            "event_type": "credit_facility",
            "event_status": "announced",
            "event_date": "2026-09-03",
            "company": "Playfly Sports",
            "borrower": "Playfly Sports",
            "lenders": ["Bain Capital"],
            "counterparty": "",
            "amounts": [
                {
                    "kind": "total_commitment",
                    "amount": 250_000_000,
                    "currency": "USD",
                    "fact_id": "f1",
                    "source_text": "$250 million",
                }
            ],
            "facility_type": "credit facility",
            "facts": [
                {
                    "fact_id": "f1",
                    "claim": (
                        "Bain Capital provided Playfly Sports with a $250 million "
                        "credit facility."
                    ),
                    "exact_quote": (
                        "Bain Capital provided Playfly Sports with a $250 million credit facility"
                    ),
                    "source_url": "https://abfjournal.example/playfly-credit",
                }
            ],
            "confidence": 82,
            "editorial_score": 88,
            "rationale": "The direct article names the parties, amount, and facility.",
        }
    )
    client = _ScriptedClient([output])

    result = await EvidenceAnalyst(client).analyze(candidate)  # type: ignore[arg-type]

    assert result["decision"] == "KEEP"
    assert result["section"] == "deal_tape"
    assert result["confidence"] == 82


@pytest.mark.asyncio
async def test_evidence_repairs_ai_company_equity_round_into_runway_watch() -> None:
    candidate = {
        "candidate_id": "S099:round-1",
        "source_id": "S099",
        "source_name": "Official company news",
        "title": "Wonderful raises $550 million Series C",
        "summary_text": "Wonderful raised a $550 million Series C financing round.",
        "body_text": (
            "Wonderful raised a $550 million Series C financing round on September 2, "
            "2026 to expand its enterprise AI platform."
        ),
        "canonical_url": "https://wonderful.example/news/series-c",
        "published_at": "2026-09-02T08:00:00Z",
        "primary_source": True,
        "discovery_only": False,
    }
    invalid = _valid_output()
    invalid.update(
        {
            "candidate_id": "S099:round-1",
            "section": "ai_radar",
            "event_type": "equity_financing",
            "event_status": "funded",
            "event_date": "2026-09-02",
            "company": "Wonderful",
            "amounts": [
                {
                    "kind": "equity_raised",
                    "amount": 550_000_000,
                    "currency": "USD",
                    "fact_id": "f1",
                    "source_text": "$550 million",
                }
            ],
            "facts": [
                {
                    "fact_id": "f1",
                    "claim": "Wonderful raised a $550 million Series C financing round.",
                    "exact_quote": (
                        "Wonderful raised a $550 million Series C financing round on "
                        "September 2, 2026"
                    ),
                    "source_url": "https://wonderful.example/news/series-c",
                }
            ],
        }
    )
    repaired = deepcopy(invalid)
    repaired["section"] = "runway_watch"
    client = _ScriptedClient([invalid, repaired])

    result = await EvidenceAnalyst(client).analyze(candidate)  # type: ignore[arg-type]

    assert result["section"] == "runway_watch"
    assert len(client.calls) == 2
    assert "EQUITY_RAISE_REQUIRES_RUNWAY_WATCH" in client.calls[1]["messages"][-1]["content"]


def test_event_from_analysis_builds_strict_event_and_keeps_amount_kinds_separate() -> None:
    candidate = CandidateV1(
        run_id="run-1",
        candidate_id="S054:item-1",
        source_id="S054",
        source_job_key="S054",
        source_name="OpenAI News",
        section_hint=DigestSection.AI_RADAR,
        source_class=SourceTier.A,
        parser_lane=ParserLane.RSS_ATOM,
        priority=Priority.P0,
        primary_source=True,
        discovery_only=False,
        retrieved_at=datetime(2026, 8, 25, 8, tzinfo=UTC),
        item_id="item-1",
        raw_url="https://openai.com/index/model-x/",
        canonical_url="https://openai.com/index/model-x/",
        title="OpenAI launches Model X",
        published_at=datetime(2026, 8, 25, 8, tzinfo=UTC),
        summary_text="The launch adds a lower-cost API tier.",
        body_text=(
            "OpenAI launched Model X on August 25, 2026. The company committed "
            "$100 million to supporting the new API tier. Initial funding was $25 million."
        ),
        content_sha256="a" * 64,
        dedupe_key="S054:item-1",
    )
    analysis = _valid_output()
    analysis["amounts"][0]["kind"] = "total_commitment"
    analysis["amounts"].append(
        {
            "kind": "initial_funding",
            "amount": 25_000_000,
            "currency": "USD",
            "fact_id": "f3",
            "source_text": "$25 million",
        }
    )
    analysis["facts"].append(
        {
            "fact_id": "f3",
            "claim": "Initial funding was $25 million.",
            "exact_quote": "Initial funding was $25 million.",
            "source_url": "https://openai.com/index/model-x/",
        }
    )
    analyzed_at = datetime(2026, 8, 25, 9, tzinfo=UTC)

    event = event_from_analysis(candidate, analysis, analyzed_at=analyzed_at)

    assert event is not None
    assert event.event_id == candidate.candidate_id
    assert event.run_id == "run-1"
    assert event.source_job_key == "S054"
    assert event.total_commitment == 100_000_000
    assert event.initial_funding == 25_000_000
    assert event.currency == "USD"
    assert event.source_ids == ("S054",)
    assert {fact.source_id for fact in event.facts} == {"S054"}
    assert event.analyzed_at == analyzed_at


def test_event_from_analysis_returns_none_for_non_keep_decision() -> None:
    candidate_data = _candidate()
    candidate = CandidateV1(
        run_id="run-2",
        candidate_id=candidate_data["candidate_id"],
        source_id="S054",
        source_job_key="S054",
        source_name="OpenAI News",
        section_hint=DigestSection.AI_RADAR,
        source_class=SourceTier.A,
        parser_lane=ParserLane.RSS_ATOM,
        priority=Priority.P0,
        primary_source=True,
        discovery_only=False,
        retrieved_at=datetime(2026, 8, 25, 8, tzinfo=UTC),
        item_id="item-1",
        raw_url=candidate_data["canonical_url"],
        canonical_url=candidate_data["canonical_url"],
        title=candidate_data["title"],
        published_at=datetime(2026, 8, 25, 8, tzinfo=UTC),
        summary_text=candidate_data["summary_text"],
        body_text=candidate_data["body_text"],
        content_sha256="b" * 64,
        dedupe_key="S054:item-1",
    )
    analysis = _valid_output()
    analysis.update({"decision": "DROP", "section": "none"})

    assert event_from_analysis(candidate, analysis) is None
