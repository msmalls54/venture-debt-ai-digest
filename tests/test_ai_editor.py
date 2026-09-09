from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest

from vdai.editor import DailyEditor, EditorialRejected, _event_packet, edition_from_draft
from vdai.models import (
    DigestRun,
    DigestRunStatus,
    DigestSection,
    EventStatus,
    EventV1,
    EvidenceDecision,
    Fact,
    SourceTier,
)


class _ScriptedClient:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return deepcopy(self.outputs[len(self.calls) - 1])


def _event() -> EventV1:
    return EventV1(
        event_id="S054:item-1",
        candidate_id="S054:item-1",
        run_id="collector-run",
        source_id="S054",
        source_job_key="S054",
        decision=EvidenceDecision.KEEP,
        section=DigestSection.AI_RADAR,
        event_type="model_launch",
        event_status=EventStatus.LAUNCHED,
        company="OpenAI",
        evidence_tier=SourceTier.A,
        facts=(
            Fact(
                fact_id="f1",
                claim="OpenAI launched Model X with a lower-cost API tier.",
                exact_quote="OpenAI launched Model X with a lower-cost API tier.",
                source_id="S054",
                source_url="https://openai.com/index/model-x/",
                evidence_tier=SourceTier.A,
            ),
        ),
        source_ids=("S054",),
        confidence=96,
        editorial_score=72,
        analyzed_at=datetime(2026, 8, 25, 9, tzinfo=UTC),
    )


def _deal_event() -> EventV1:
    return EventV1(
        event_id="S003:item-2",
        candidate_id="S003:item-2",
        run_id="collector-run",
        source_id="S003",
        source_job_key="S003",
        decision=EvidenceDecision.KEEP,
        section=DigestSection.DEAL_TAPE,
        event_type="venture_debt",
        event_status=EventStatus.CLOSED,
        company="Borrower",
        borrower="Borrower",
        evidence_tier=SourceTier.A,
        facts=(
            Fact(
                fact_id="f2",
                claim="Borrower closed a credit facility.",
                exact_quote="Borrower closed a credit facility.",
                source_id="S003",
                source_url="https://borrower.example/credit-facility/",
                evidence_tier=SourceTier.A,
            ),
        ),
        source_ids=("S003",),
        confidence=94,
        editorial_score=78,
        analyzed_at=datetime(2026, 8, 25, 9, tzinfo=UTC),
    )


def test_editor_admits_strong_exact_quote_secondary_evidence() -> None:
    secondary = _deal_event().model_copy(
        update={"evidence_tier": SourceTier.C, "confidence": 82}
    )
    secondary = secondary.model_copy(
        update={
            "facts": tuple(
                fact.model_copy(update={"evidence_tier": SourceTier.C})
                for fact in secondary.facts
            )
        }
    )

    packet = _event_packet(secondary)

    assert packet is not None
    assert packet["evidence_tier"] == "C"
    assert packet["confidence"] == 82


@pytest.mark.asyncio
async def test_fifteen_stories_beyond_old_editor_pool_round_trip_to_saved_run() -> None:
    events = [
        _event().model_copy(update={"event_id": f"S054:item-{i}", "candidate_id": f"S054:item-{i}"})
        for i in range(1, 61)
    ]
    draft = _valid_draft()
    template = draft["ai_radar"][0]
    draft["ai_radar"] = [dict(template, event_id=f"S054:item-{i}") for i in range(31, 46)]
    client = _ScriptedClient([draft])
    result = await DailyEditor(client).draft(events, edition_date="2026-08-25")
    assert result["story_count"] == 15
    assert len(client.calls) == 1
    assert len(json.loads(client.calls[0]["messages"][1]["content"])["events"]) == 60
    edition = edition_from_draft(result, events, run_id="RUN-expanded")
    run = DigestRun(
        run_id=edition.run_id,
        digest_date=edition.edition_date,
        started_at=edition.generated_at,
        selected_count=15,
        agent_model="google/gemini-3.8-flash",
        send_id="expanded-test",
        recipient_group="test",
        status=DigestRunStatus.READY_TO_SEND,
        edition_json=edition.model_dump(mode="json"),
    )
    assert len(run.edition_json["stories"]) == 15


@pytest.mark.asyncio
async def test_editor_reserves_high_reasoning_for_final_draft() -> None:
    client = _ScriptedClient([_valid_draft()])

    await DailyEditor(client).draft([_event()], edition_date="2026-08-25")  # type: ignore[arg-type]

    assert client.calls[0]["reasoning_effort"] == "high"


def test_editor_rejects_low_confidence_secondary_evidence() -> None:
    secondary = _deal_event().model_copy(
        update={"evidence_tier": SourceTier.C, "confidence": 79}
    )

    assert _event_packet(secondary) is None


def _valid_draft() -> dict[str, Any]:
    return {
        "edition": {
            "kicker": "VENTURE DEBT + AI",
            "headline": "OpenAI tightens the model-cost race",
            "deck": "One primary-source launch clears the bar today.",
        },
        "lead": None,
        "deal_tape": [],
        "competitive_field": [],
        "ai_radar": [
            {
                "event_id": "S054:item-1",
                "headline": "OpenAI — Model X lands with a lower-cost API tier",
                "dek": "OpenAI launched Model X with a lower-cost API tier.",
                "why_it_matters": "The lower-cost API tier raises the pressure on model pricing.",
                "fact_ids": ["f1"],
                "source_ids": ["S054"],
                "entity_names": ["OpenAI"],
            }
        ],
        "runway_watch": [],
        "quiet_day": False,
    }


def _two_story_draft() -> dict[str, Any]:
    draft = _valid_draft()
    draft["deal_tape"] = [
        {
            "event_id": "S003:item-2",
            "headline": "Borrower — credit facility closes",
            "dek": "Borrower closed a credit facility.",
            "why_it_matters": "The facility adds runway without changing the equity price.",
            "fact_ids": ["f2"],
            "source_ids": ["S003"],
            "entity_names": ["Borrower"],
        }
    ]
    return draft


@pytest.mark.asyncio
async def test_editor_repairs_unknown_source_once_and_never_prompts_with_recipient() -> None:
    invalid = _valid_draft()
    invalid["ai_radar"][0]["source_ids"] = ["INVENTED"]
    client = _ScriptedClient([invalid, _valid_draft()])
    event_payload = _event().model_dump(mode="json")
    event_payload["recipient_email"] = "owner@example.com"

    result = await DailyEditor(client).draft(  # type: ignore[arg-type]
        [event_payload],
        edition_date="2026-08-25",
        forbidden_prompt_values=["owner@example.com"],
    )

    assert result["story_count"] == 1
    assert result["validated_stories"][0]["citations"][0]["url"].startswith("https://")
    assert len(client.calls) == 2
    assert "UNSUPPORTED_SOURCE_ID" in client.calls[1]["messages"][-1]["content"]
    prompts = "\n".join(message["content"] for call in client.calls for message in call["messages"])
    assert "owner@example.com" not in prompts
    assert "Material finance-platform developments" in prompts


@pytest.mark.asyncio
async def test_editor_fails_closed_after_one_repair_for_invented_number() -> None:
    invalid = _valid_draft()
    invalid["ai_radar"][0]["dek"] = "OpenAI priced the tier at $900 million."
    client = _ScriptedClient([invalid, invalid])

    with pytest.raises(EditorialRejected, match="NUMBER_NOT_IN_SELECTED_FACTS"):
        await DailyEditor(client).draft(  # type: ignore[arg-type]
            [_event()], edition_date="2026-08-25"
        )

    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_editor_drops_one_invalid_story_after_repair_and_keeps_valid_section() -> None:
    mixed = _two_story_draft()
    mixed["ai_radar"][0]["source_ids"] = ["INVENTED"]
    repaired = deepcopy(mixed)
    repaired["edition"]["headline"] = "Borrower closes a credit facility"
    repaired["edition"]["deck"] = "One venture-debt transaction clears the bar today."
    client = _ScriptedClient([mixed, repaired])

    result = await DailyEditor(client).draft(  # type: ignore[arg-type]
        [_event(), _deal_event()], edition_date="2026-08-25"
    )

    assert len(client.calls) == 2
    assert "UNSUPPORTED_SOURCE_ID" in client.calls[1]["messages"][-1]["content"]
    assert result["story_count"] == 1
    assert result["ai_radar"] == []
    assert [story["event_id"] for story in result["deal_tape"]] == ["S003:item-2"]
    assert [story["event_id"] for story in result["validated_stories"]] == ["S003:item-2"]


@pytest.mark.asyncio
async def test_editor_repairs_a_seven_story_draft_when_eight_events_are_available() -> None:
    events = []
    for index in range(1, 9):
        section = DigestSection.AI_RADAR if index <= 4 else DigestSection.RUNWAY_WATCH
        events.append(
            _event().model_copy(
                update={
                    "event_id": f"S054:item-{index}",
                    "candidate_id": f"S054:item-{index}",
                    "section": section,
                }
            )
        )

    def story(event_id: str) -> dict[str, Any]:
        item = deepcopy(_valid_draft()["ai_radar"][0])
        item["event_id"] = event_id
        return item

    first = _valid_draft()
    first["lead"] = story("S054:item-1")
    first["ai_radar"] = [story(f"S054:item-{index}") for index in range(2, 5)]
    first["runway_watch"] = [story(f"S054:item-{index}") for index in range(5, 8)]
    repaired = deepcopy(first)
    repaired["runway_watch"].append(story("S054:item-8"))
    client = _ScriptedClient([first, repaired])

    result = await DailyEditor(client).draft(  # type: ignore[arg-type]
        events, edition_date="2026-08-25"
    )

    assert len(client.calls) == 2
    assert "STORY_FLOOR_NOT_MET" in client.calls[1]["messages"][-1]["content"]
    assert result["story_count"] == 8


@pytest.mark.asyncio
async def test_editor_does_not_drop_global_edition_errors_after_repair() -> None:
    invalid = _two_story_draft()
    invalid["edition"]["deck"] = "A revolutionary briefing clears the bar today."
    client = _ScriptedClient([invalid, invalid])

    with pytest.raises(EditorialRejected, match="BANNED_EDITION_COPY"):
        await DailyEditor(client).draft(  # type: ignore[arg-type]
            [_event(), _deal_event()], edition_date="2026-08-25"
        )

    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_editor_replaces_edition_headline_about_an_unselected_event() -> None:
    invalid = _two_story_draft()
    invalid["edition"]["headline"] = "Borrower closes a $926 million facility"
    invalid["deal_tape"] = []
    client = _ScriptedClient([invalid])

    result = await DailyEditor(client).draft(  # type: ignore[arg-type]
        [_event(), _deal_event()], edition_date="2026-08-25"
    )

    assert result["edition"]["headline"] == invalid["ai_radar"][0]["headline"]
    assert result["edition"]["deck"] == (
        "Verified developments with source-linked commentary."
    )
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_editor_allows_ordinary_capitalized_sentence_starters() -> None:
    draft = _valid_draft()
    draft["ai_radar"][0]["dek"] = (
        "Lower-cost access increases pressure on model pricing."
    )
    draft["ai_radar"][0]["why_it_matters"] = (
        "Because the API tier costs less, pricing pressure rises. "
        "This makes execution and adoption the next things to watch."
    )
    client = _ScriptedClient([draft])

    result = await DailyEditor(client).draft(  # type: ignore[arg-type]
        [_event()], edition_date="2026-08-25"
    )

    assert result["story_count"] == 1
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_editor_still_rejects_unknown_proper_name_inside_sentence() -> None:
    invalid = _valid_draft()
    invalid["ai_radar"][0]["why_it_matters"] = (
        "The launch puts Microsoft under additional pricing pressure."
    )
    client = _ScriptedClient([invalid, invalid])

    with pytest.raises(EditorialRejected, match="POSSIBLE_INVENTED_ENTITY"):
        await DailyEditor(client).draft(  # type: ignore[arg-type]
            [_event()], edition_date="2026-08-25"
        )

    assert len(client.calls) == 2
    assert "POSSIBLE_INVENTED_ENTITY_TOKEN_MICROSOFT" in (
        client.calls[1]["messages"][-1]["content"]
    )


def test_edition_from_draft_builds_deterministic_story_ids_ranks_and_citations() -> None:
    validated = {
        "edition_date": "2026-08-25",
        "edition": _valid_draft()["edition"],
        "validated_stories": [
            {
                "section": "ai_radar",
                **_valid_draft()["ai_radar"][0],
                "citations": [
                    {
                        "source_id": "S054",
                        "url": "https://invented.example/model-output-must-be-ignored",
                    }
                ],
            }
        ],
        "word_count": 42,
    }
    generated_at = datetime(2026, 8, 25, 10, tzinfo=UTC)

    edition = edition_from_draft(
        validated,
        [_event()],
        run_id="edition-2026-08-25",
        generated_at=generated_at,
    )

    assert edition.run_id == "edition-2026-08-25"
    assert edition.generated_at == generated_at
    assert len(edition.stories) == 1
    story = edition.stories[0]
    assert story.published_story_id == ("edition-2026-08-25:S054:item-1:ai_radar:1")
    assert story.rank == 1
    assert story.raw_event_id == "S054:item-1"
    assert story.entity_names == ("OpenAI",)
    assert story.citations[0].source_id == "S054"
    assert str(story.citations[0].url) == "https://openai.com/index/model-x/"
    assert story.citations[0].fact_ids == ("f1",)
