from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from vdai.models import DigestSection, ParserLane
from vdai.research import ResearchError, WebResearchScout, _grounded_search_url
from vdai.research import _ResearchLead as ResearchLead


class _OpenRouter:
    def __init__(self, output: dict[str, Any] | list[dict[str, Any]]) -> None:
        self.outputs = output if isinstance(output, list) else [output]
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        output = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]
        if (citations := kwargs.get("search_citations")) is not None:
            citations.extend(
                {"url": lead["source_url"], "title": lead["headline"]}
                for lead in output.get("leads", [])
                if isinstance(lead, dict) and lead.get("source_url")
            )
        return output


def test_research_replaces_an_invented_path_with_same_host_search_citation() -> None:
    lead = ResearchLead(
        headline="JetZero Closes $100 Million Financing Facility to Advance the Z4 Program",
        source_url=(
            "https://www.jetzero.aero/press/"
            "jetzero-closes-100-million-financing-facility-to-advance-the-z4-program"
        ),
        publisher="JetZero",
        category="venture_debt",
        why_relevant="A material venture-debt transaction.",
    )

    grounded = _grounded_search_url(
        lead,
        [
            {
                "url": "https://www.jetzero.aero/jetzero-closes-100-million-financing",
                "title": "JetZero Closes $100 Million Financing",
            }
        ],
    )

    assert grounded == "https://www.jetzero.aero/jetzero-closes-100-million-financing"


@pytest.mark.asyncio
async def test_research_scout_uses_bounded_web_search_and_builds_shadow_jobs() -> None:
    client = _OpenRouter(
        {
            "leads": [
                {
                    "headline": "Lender launches a growth-credit strategy",
                    "source_url": "https://lender.example/news/growth-credit",
                    "publisher": "Lender",
                    "category": "venture_debt",
                    "why_relevant": "A new lending capability matters to the market.",
                },
                {
                    "headline": "Duplicate",
                    "source_url": "https://lender.example/news/growth-credit",
                    "publisher": "Lender",
                    "category": "finance_platform",
                    "why_relevant": "Duplicate result.",
                },
                {
                    "headline": "Unsafe local result",
                    "source_url": "https://localhost/private",
                    "publisher": "Unsafe",
                    "category": "ai",
                    "why_relevant": "Must be discarded.",
                },
            ]
        }
    )

    jobs = await WebResearchScout(client).discover_jobs(  # type: ignore[arg-type]
        start=datetime(2026, 9, 1, tzinfo=UTC),
        end=datetime(2026, 9, 2, tzinfo=UTC),
        seed_headlines=["KKR expands its credit platform"],
    )

    assert len(jobs) == 1
    assert len(client.calls) == 5
    assert all(call["web_search"] is True for call in client.calls)
    assert "KKR expands its credit platform" in client.calls[0]["messages"][1]["content"]
    assert jobs[0].parser_lane is ParserLane.HTML
    assert jobs[0].source.section is DigestSection.DEAL_TAPE
    assert jobs[0].source.allowed_domains == ("lender.example",)
    assert jobs[0].write_mode == "shadow"
    assert jobs[0].source.max_items == 1
    assert jobs[0].source.primary_source is False
    assert jobs[0].source.discovery_only is False


@pytest.mark.asyncio
async def test_research_scout_rejects_non_https_and_credentialed_urls() -> None:
    client = _OpenRouter(
        {
            "leads": [
                {
                    "headline": "HTTP",
                    "source_url": "http://example.com/item",
                    "publisher": "Example",
                    "category": "ai",
                    "why_relevant": "Unsafe scheme.",
                },
                {
                    "headline": "Credentialed",
                    "source_url": "https://user:pass@example.com/item",
                    "publisher": "Example",
                    "category": "ai",
                    "why_relevant": "Unsafe authority.",
                },
            ]
        }
    )

    with pytest.raises(ResearchError, match="all bounded web research lanes"):
        await WebResearchScout(client).discover_jobs(  # type: ignore[arg-type]
            start=datetime(2026, 9, 1, tzinfo=UTC),
            end=datetime(2026, 9, 2, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_research_scout_fails_loudly_on_empty_search_packet() -> None:
    client = _OpenRouter({"leads": []})

    with pytest.raises(ResearchError, match="all bounded web research lanes"):
        await WebResearchScout(client).discover_jobs(  # type: ignore[arg-type]
            start=datetime(2026, 9, 1, tzinfo=UTC),
            end=datetime(2026, 9, 2, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_research_scout_repairs_one_schema_invalid_lead_packet_without_researching() -> None:
    client = _OpenRouter(
        [
            {
                "leads": [
                    {
                        "headline": "A material credit announcement",
                        "source_url": "https://lender.example/news/credit",
                        "publisher": "Lender",
                        "category": "private_credit",
                        "why_relevant": "Material to private credit.",
                    }
                ]
            },
            {
                "leads": [
                    {
                        "headline": "A material credit announcement",
                        "source_url": "https://lender.example/news/credit",
                        "publisher": "Lender",
                        "category": "venture_debt",
                        "why_relevant": "Material to private credit.",
                    }
                ]
            },
        ]
    )

    jobs = await WebResearchScout(client).discover_jobs(  # type: ignore[arg-type]
        start=datetime(2026, 9, 1, tzinfo=UTC),
        end=datetime(2026, 9, 2, tzinfo=UTC),
    )

    assert len(jobs) == 1
    assert len(client.calls) == 6
    assert client.calls[0]["web_search"] is True
    assert client.calls[1]["web_search"] is False
    assert "untrusted_prior_output" in client.calls[1]["messages"][-1]["content"]
