from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from vdai.editor import edition_from_draft
from vdai.models import (
    CandidateV1,
    Citation,
    DigestRun,
    DigestRunStatus,
    DigestSection,
    Edition,
    EventV1,
    EvidenceDecision,
    Fact,
    HealthStatus,
    ParserLane,
    Priority,
    SourceHealth,
    SourceTier,
    Story,
)
from vdai.sheets import (
    GoogleSheetsRepository,
    SheetSchemaError,
    SheetSecurityError,
    serialize_sheet_value,
)

NOW = datetime(2026, 8, 25, 15, 0, tzinfo=UTC)


class FakeWorksheet:
    def __init__(self, title: str, rows: list[list[str]]):
        self.title = title
        self.rows = [list(row) for row in rows]
        self.read_count = 0
        self.append_rows_count = 0

    def get_all_values(self) -> list[list[str]]:
        self.read_count += 1
        return [list(row) for row in self.rows]

    def append_row(self, values: list[Any], **_: Any) -> None:
        self.rows.append([str(value) for value in values])

    def append_rows(self, values: list[list[Any]], **_: Any) -> None:
        self.append_rows_count += 1
        self.rows.extend([[str(value) for value in row] for row in values])

    def update(self, values: list[list[Any]], range_name: str, **_: Any) -> None:
        match = re.search(r"(\d+):[A-Z]+(\d+)$", range_name)
        assert match and match.group(1) == match.group(2)
        row_index = int(match.group(1)) - 1
        self.rows[row_index] = [str(value) for value in values[0]]


class FakeSpreadsheet:
    def __init__(self, worksheets: dict[str, FakeWorksheet]):
        self.worksheets = worksheets

    def worksheet(self, title: str) -> FakeWorksheet:
        return self.worksheets[title]


def repository(**tabs: list[list[str]]) -> tuple[GoogleSheetsRepository, dict[str, FakeWorksheet]]:
    worksheets = {name: FakeWorksheet(name, rows) for name, rows in tabs.items()}
    return GoogleSheetsRepository(FakeSpreadsheet(worksheets)), worksheets


def candidate() -> CandidateV1:
    return CandidateV1(
        run_id="run-1",
        candidate_id="candidate-1",
        source_id="S001",
        source_job_key="S001",
        source_name="Borrower newsroom",
        section_hint=DigestSection.DEAL_TAPE,
        source_class=SourceTier.A,
        parser_lane=ParserLane.RSS_ATOM,
        priority=Priority.P0,
        primary_source=True,
        discovery_only=False,
        retrieved_at=NOW,
        item_id="release-1",
        raw_url="https://borrower.example/releases/1",
        canonical_url="https://borrower.example/releases/1",
        title="Borrower closes facility",
        published_at=NOW,
        summary_text="Borrower closed a facility.",
        body_text="Borrower closed a $25 million facility with Lender.",
        content_sha256="a" * 64,
        dedupe_key="candidate-1",
    )


def fact() -> Fact:
    return Fact(
        fact_id="fact-1",
        claim="Borrower closed a facility.",
        exact_quote="closed a $25 million facility",
        source_id="S001",
        source_url="https://borrower.example/releases/1",
        evidence_tier=SourceTier.A,
    )


def event() -> EventV1:
    return EventV1(
        event_id="candidate-1",
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


def story_and_edition() -> tuple[Story, Edition]:
    story = Story(
        published_story_id="story-1",
        candidate_id="candidate-1",
        raw_event_id="candidate-1",
        section=DigestSection.DEAL_TAPE,
        rank=1,
        headline="Borrower — $25M facility closed",
        dek="The company added runway.",
        why_it_matters="Lender won a visible mandate.",
        company="Borrower",
        fact_ids=("fact-1",),
        source_ids=("S001",),
        citations=(
            Citation(
                source_id="S001",
                url="https://borrower.example/releases/1",
                publisher="Borrower",
                fact_ids=("fact-1",),
            ),
        ),
    )
    return story, Edition(
        run_id="run-1",
        edition_date=date(2026, 8, 25),
        subject="Venture Debt + AI — August 25",
        stories=(story,),
        word_count=50,
        generated_at=NOW,
    )


def source_headers() -> list[str]:
    return [
        "source_id",
        "section",
        "organization",
        "source_name",
        "url",
        "method",
        "cadence",
        "source_tier",
        "priority",
        "active",
        "allowed_domains",
        "primary_source",
        "discovery_only",
        "max_items",
        "max_pages",
        "max_bytes",
        "parser_version",
    ]


def test_loaders_are_header_driven_and_recipient_rules_are_private() -> None:
    source_values = [
        "S001",
        "Deal Tape",
        "Borrower",
        "Borrower newsroom",
        "https://borrower.example/news",
        "RSS",
        "daily",
        "3-primary",
        "P0",
        "TRUE",
        "borrower.example",
        "TRUE",
        "FALSE",
        "25",
        "3",
        "1000000",
        "rss.v1",
    ]
    formatted_blank_source = [""] * len(source_headers())
    formatted_blank_source[11] = "FALSE"
    formatted_blank_source[12] = "FALSE"
    repo, _ = repository(
        **{
            "Sources": [source_headers(), source_values, formatted_blank_source],
            "Settings": [
                ["description", "value", "setting"],
                ["safe", "TRUE", "test_mode"],
                ["threshold", "0.9", "source_health_threshold"],
            ],
            "Company Watchlist": [
                ["Company Number", "SEC CIK", "Name", "Active"],
                ["ab123456", "320193", "Borrower", "TRUE"],
                ["zz999999", "1747777", "Inactive Borrower", "FALSE"],
            ],
            "Deposit Watch": [
                ["Name", "Deposit Competitor", "Active"],
                ["Brex", "TRUE", "TRUE"],
                ["Paused bank", "TRUE", "FALSE"],
            ],
            "Recipients": [
                ["display_name", "email", "active"],
                ["Owner", " OWNER@example.com ", ""],
                ["Duplicate", "owner@example.com", "TRUE"],
                ["Invalid", "not-an-email", "TRUE"],
                ["Sender", "mikesupdateagent@agentmail.to", "TRUE"],
                ["Paused", "paused@example.com", "FALSE"],
            ],
        }
    )
    assert repo.load_sources()[0].source_tier is SourceTier.A
    assert repo.load_settings()["test_mode"] is True
    assert repo.load_settings()["source_health_threshold"] == 0.9
    company = repo.load_company_watchlist()[0]
    assert company["companies_house_number"] == "ab123456"
    assert company["sec_cik"] == "320193"
    assert company["active"] is True
    assert len(repo.load_company_watchlist()) == 1
    assert len(repo.load_company_watchlist(active_only=False)) == 2
    assert repo.load_deposit_watch()[0]["deposit_competitor"] is True
    assert len(repo.load_deposit_watch()) == 1
    assert len(repo.load_deposit_watch(active_only=False)) == 2
    batch = repo.load_recipients(exclude_addresses=("mikesupdateagent@agentmail.to",))
    assert [str(recipient.email) for recipient in batch.recipients] == ["owner@example.com"]
    assert batch.duplicate_count == 1
    assert batch.invalid_count == 1
    assert batch.excluded_count == 2
    assert "owner@example.com" not in repr(batch.model_payload())


def test_sheet_settings_reject_secrets_without_echoing_values() -> None:
    repo, _ = repository(
        **{"Settings": [["setting", "value"], ["openrouter_api_key", "very-secret-value"]]}
    )
    with pytest.raises(SheetSecurityError) as error:
        repo.load_settings()
    assert "very-secret-value" not in str(error.value)


def test_recipient_loader_enforces_private_bcc_maximum_49() -> None:
    rows = [["email"], *[[f"person{index}@example.com"] for index in range(51)]]
    repo, _ = repository(**{"Recipients": rows})
    batch = repo.load_recipients()
    assert len(batch.recipients) == 49
    assert batch.overflow_count == 2


def test_formula_strings_are_escaped_and_complex_values_are_stable_json() -> None:
    assert serialize_sheet_value("=IMPORTDATA('bad')").startswith("'")
    assert serialize_sheet_value({"b": 2, "a": [1]}) == '{"a":[1],"b":2}'


def test_raw_event_upsert_uses_header_name_and_durable_event_key() -> None:
    repo, tabs = repository(
        **{
            "Raw Events": [
                ["status", "notes", "event_id", "source_name", "candidate_id", "content_hash"]
            ]
        }
    )
    assert repo.upsert_raw_event(event(), candidate())[0] == "appended"
    assert tabs["Raw Events"].rows[1][2] == "candidate-1"
    assert repo.raw_event_exists(event_id="candidate-1") is True
    assert repo.candidate_exists(candidate()) is True
    assert repo.upsert_raw_event(event(), candidate())[0] == "updated"
    assert len(tabs["Raw Events"].rows) == 2


def test_health_story_and_digest_run_writes_are_idempotent() -> None:
    health_headers = [
        "notes",
        "status",
        "candidate_id",
        "failure_stage",
        "source_job_key",
        "run_id",
    ]
    repo, tabs = repository(
        **{
            "Source Health": [health_headers],
            "Published Stories": [
                ["headline", "published_story_id", "status", "send_id", "source_ids"]
            ],
            "Digest Runs": [["status", "run_id", "section_counts_json", "subject", "send_id"]],
        }
    )
    health = SourceHealth(
        source_id="S001",
        source_job_key="S001",
        last_attempt_at=NOW,
        last_success_at=NOW,
        status=HealthStatus.SUCCESS,
        item_count=1,
        parser_version="rss.v1",
        updated_at=NOW,
        run_id="run-1",
        source_name="Borrower newsroom",
        checked_at=NOW,
        failure_stage="source_complete",
        parser_lane=ParserLane.RSS_ATOM,
    )
    assert repo.append_source_health(health)[0] == "appended"
    assert repo.append_source_health(health)[0] == "updated"
    story, edition = story_and_edition()
    assert (
        repo.upsert_published_story(
            story,
            edition,
            send_id="vdai-2026-08-25-owner",
            recipient_group="owner_test",
            status=DigestRunStatus.READY_TO_SEND,
            included_at=NOW,
        )[0]
        == "appended"
    )
    assert (
        repo.upsert_published_story(
            story,
            edition,
            send_id="vdai-2026-08-25-owner",
            recipient_group="owner_test",
            status=DigestRunStatus.SENT,
            included_at=NOW,
        )[0]
        == "updated"
    )
    digest = DigestRun(
        run_id="run-1",
        digest_date=date(2026, 8, 25),
        started_at=NOW,
        completed_at=NOW,
        health_status="HEALTHY",
        candidate_count=1,
        selected_count=1,
        section_counts={"deal_tape": 1},
        agent_model="google/gemini-3.8-flash",
        subject="Venture Debt + AI",
        send_id="vdai-2026-08-25-owner",
        recipient_group="owner_test",
        status=DigestRunStatus.READY_TO_SEND,
    )
    assert repo.upsert_digest_run(digest)[0] == "appended"
    assert repo.upsert_digest_run(digest)[0] == "updated"
    assert len(tabs["Source Health"].rows) == 2
    assert len(tabs["Published Stories"].rows) == 2
    assert len(tabs["Digest Runs"].rows) == 2


def test_large_ready_edition_is_compressed_and_round_trips() -> None:
    _, edition = story_and_edition()
    large_edition = edition.model_copy(
        update={
            "html": "<p>" + ("Rendered HTML body. " * 3_000) + "</p>",
            "text": ("Rendered text body. " * 2_000).strip(),
        }
    )
    run = DigestRun(
        run_id="run-large",
        digest_date=large_edition.edition_date,
        started_at=NOW,
        health_status="HEALTHY",
        candidate_count=1,
        selected_count=1,
        section_counts={"deal_tape": 1},
        agent_model="google/gemini-3.8-flash",
        subject=large_edition.subject,
        send_id="vdai-2026-08-25-large",
        recipient_group="owner_test",
        status=DigestRunStatus.READY_TO_SEND,
        edition_json=large_edition.model_dump(mode="json"),
    )
    repo, tabs = repository(
        **{
            "Digest Runs": [["run_id", "status", "send_id", "edition_json"]],
        }
    )

    repo.upsert_digest_run(run)

    persisted = tabs["Digest Runs"].rows[1][3]
    assert len(persisted) < 50_000
    assert "vdai-edition+gzip+base64.v1" in persisted
    restored = repo.load_ready_edition(run.send_id)
    assert restored is not None
    assert restored.model_dump(mode="json") == large_edition.model_dump(mode="json")


def test_latest_persisted_edition_prefers_newest_sent_or_ready_run() -> None:
    _, older_edition = story_and_edition()
    newer_edition = older_edition.model_copy(
        update={
            "run_id": "run-newer",
            "subject": "Newer saved brief",
            "generated_at": NOW + timedelta(hours=2),
        }
    )
    older = DigestRun(
        run_id="run-older",
        digest_date=older_edition.edition_date,
        started_at=NOW,
        completed_at=NOW,
        agent_model="google/gemini-3.8-flash",
        send_id="vdai-older",
        recipient_group="owner_test",
        status=DigestRunStatus.SENT,
        edition_json=older_edition.model_dump(mode="json"),
    )
    newer = DigestRun(
        run_id="run-newer",
        digest_date=newer_edition.edition_date,
        started_at=NOW + timedelta(hours=2),
        agent_model="google/gemini-3.8-flash",
        send_id="vdai-newer",
        recipient_group="owner_test",
        status=DigestRunStatus.READY_TO_SEND,
        edition_json=newer_edition.model_dump(mode="json"),
    )
    repo, _ = repository(
        **{
            "Digest Runs": [
                ["run_id", "status", "started_at", "completed_at", "edition_json"]
            ],
        }
    )
    repo.upsert_digest_run(older)
    repo.upsert_digest_run(newer)

    restored = repo.load_latest_persisted_edition()

    assert restored is not None
    assert restored.run_id == "run-newer"
    assert restored.subject == "Newer saved brief"


def test_public_editor_queries_reconstruct_facts_history_send_and_latest_health() -> None:
    old = (NOW - timedelta(days=10)).isoformat()
    recent = (NOW - timedelta(hours=2)).isoformat()
    health_headers = [
        "source_id",
        "source_job_key",
        "last_attempt_at",
        "last_success_at",
        "status",
        "item_count",
        "consecutive_failures",
        "parser_version",
        "updated_at",
        "run_id",
        "source_name",
        "checked_at",
        "health_status",
        "failure_stage",
        "reason_codes",
        "candidate_id",
        "parser_lane",
    ]
    repo, _ = repository(
        **{
            "Raw Events": [
                [
                    "event_id",
                    "candidate_id",
                    "content_hash",
                    "status",
                    "agent_decision",
                    "needs_primary_source",
                    "analysis_at",
                    "notes",
                    "fact_ids_json",
                    "evidence_snippet",
                    "source_ids_json",
                    "source_id",
                    "canonical_url",
                    "section",
                    "event_type",
                    "event_status",
                    "source_tier",
                    "source_name",
                    "company",
                    "confidence",
                    "editorial_score",
                    "run_id",
                ],
                [
                    "event-1",
                    "candidate-1",
                    "a" * 64,
                    "ELIGIBLE",
                    "KEEP",
                    "FALSE",
                    recent,
                    '{"verified_facts":[{"fact_id":"f1","claim":"claim","exact_quote":"quote"}]}',
                    '["f1"]',
                    "quote",
                    '["S001"]',
                    "S001",
                    "https://borrower.example/1",
                    "ai_radar",
                    "model_launch",
                    "launched",
                    "A",
                    "Borrower official",
                    "Borrower",
                    "95",
                    "80",
                    "collector-run",
                ],
            ],
            "Published Stories": [
                ["published_story_id", "status", "included_at", "headline"],
                ["story-1", "SENT", recent, "Recent"],
                ["story-old", "SENT", old, "Old"],
            ],
            "Digest Runs": [
                ["run_id", "send_id", "status"],
                ["run-1", "send-1", "SENT"],
            ],
            "Source Health": [
                health_headers,
                [
                    "S001",
                    "S001",
                    old,
                    old,
                    "SUCCESS",
                    "1",
                    "0",
                    "rss.v1",
                    old,
                    "run-old",
                    "Source",
                    old,
                    "SUCCESS",
                    "source_complete",
                    "[]",
                    "",
                    "rss_atom",
                ],
                [
                    "S001",
                    "S001",
                    recent,
                    recent,
                    "UNCHANGED",
                    "1",
                    "0",
                    "rss.v1",
                    recent,
                    "run-new",
                    "Source",
                    recent,
                    "UNCHANGED",
                    "source_complete",
                    "[]",
                    "",
                    "rss_atom",
                ],
            ],
        }
    )
    events = repo.load_eligible_events(NOW - timedelta(hours=30), now=NOW)
    assert len(events) == 1
    assert events[0]["facts"][0]["fact_id"] == "f1"
    assert "recipient" not in repr(events).lower()
    history = repo.load_published_history(NOW - timedelta(days=7), now=NOW)
    assert [row["published_story_id"] for row in history] == ["story-1"]
    assert repo.digest_was_sent("send-1") is True
    latest = repo.load_latest_health_by_job(["S001"])
    assert latest["S001"].status is HealthStatus.UNCHANGED


def test_source_health_filters_legacy_n8n_rows_before_strict_validation() -> None:
    headers = [
        "source_id",
        "source_job_key",
        "last_attempt_at",
        "last_success_at",
        "status",
        "item_count",
        "parser_version",
        "updated_at",
        "run_id",
        "source_name",
        "checked_at",
        "failure_stage",
    ]
    legacy_row = [
        "S-LEGACY",
        "S-LEGACY",
        NOW.isoformat(),
        "",
        "DEFERRED",
        "0",
        "legacy.n8n",
        NOW.isoformat(),
        "collect:31",
        "Legacy n8n source",
        NOW.isoformat(),
        "source_terminal",
    ]
    railway_row = [
        "S001",
        "S001",
        NOW.isoformat(),
        NOW.isoformat(),
        "SUCCESS",
        "1",
        "rss.v1",
        NOW.isoformat(),
        "RUN-20260825-150000",
        "Borrower newsroom",
        NOW.isoformat(),
        "source_complete",
    ]
    repo, worksheets = repository(**{"Source Health": [headers, legacy_row, railway_row]})
    original_rows = [list(row) for row in worksheets["Source Health"].rows]

    health = repo.load_source_health(run_prefixes=("RUN-",))

    assert [row.run_id for row in health] == ["RUN-20260825-150000"]
    assert health[0].status is HealthStatus.SUCCESS
    assert worksheets["Source Health"].rows == original_rows


def test_source_health_strictly_validates_rows_matching_run_prefix() -> None:
    headers = [
        "source_id",
        "source_job_key",
        "last_attempt_at",
        "status",
        "parser_version",
        "updated_at",
        "run_id",
        "source_name",
        "checked_at",
        "failure_stage",
    ]
    malformed_railway_row = [
        "S001",
        "S001",
        NOW.isoformat(),
        "DEFERRED",
        "rss.v1",
        NOW.isoformat(),
        "RUN-20260825-malformed",
        "Borrower newsroom",
        NOW.isoformat(),
        "source_terminal",
    ]
    repo, _ = repository(**{"Source Health": [headers, malformed_railway_row]})

    with pytest.raises(SheetSchemaError, match=r"Source Health row 2 is invalid"):
        repo.load_source_health(run_prefixes=("RUN-",))


def test_raw_event_round_trip_reconstructs_strict_editor_contract() -> None:
    raw = GoogleSheetsRepository._raw_event_row(event(), candidate())
    headers = list(raw)
    repo, _ = repository(**{"Raw Events": [headers]})
    repo.upsert_raw_event(event(), candidate())

    eligible = repo.load_eligible_events(NOW - timedelta(hours=1), now=NOW)

    assert len(eligible) == 1
    reconstructed = EventV1.model_validate({key: eligible[0][key] for key in EventV1.model_fields})
    assert reconstructed == event()
    draft = {
        "edition_date": "2026-08-25",
        "edition": {
            "kicker": "VENTURE DEBT + AI",
            "headline": "Borrower adds runway",
            "deck": "One verified facility cleared the bar.",
        },
        "validated_stories": [
            {
                "event_id": "candidate-1",
                "section": "deal_tape",
                "headline": "Borrower — $25M facility closes",
                "dek": "Borrower closed a facility.",
                "why_it_matters": "A named lender won a visible mandate.",
                "fact_ids": ["fact-1"],
                "source_ids": ["S001"],
            }
        ],
    }
    edition = edition_from_draft(draft, eligible, run_id="edition-run", generated_at=NOW)
    assert edition.stories[0].raw_event_id == "candidate-1"
    assert str(edition.stories[0].citations[0].url) == str(candidate().canonical_url)


def test_source_health_batch_uses_one_read_and_one_append_call() -> None:
    headers = [
        "source_id",
        "source_job_key",
        "last_attempt_at",
        "last_success_at",
        "status",
        "parser_version",
        "updated_at",
        "run_id",
        "source_name",
        "checked_at",
        "failure_stage",
        "candidate_id",
    ]
    repo, worksheets = repository(**{"Source Health": [headers]})
    receipts = [
        SourceHealth(
            source_id=f"S00{index}",
            source_job_key=f"S00{index}",
            last_attempt_at=NOW,
            last_success_at=NOW,
            status=HealthStatus.SUCCESS,
            parser_version="html.v1",
            updated_at=NOW,
            run_id="batch-run",
            source_name=f"Source {index}",
            checked_at=NOW,
            failure_stage="source_complete",
        )
        for index in range(1, 4)
    ]

    results = repo.append_source_health_many(receipts)

    assert len(results) == 3
    assert worksheets["Source Health"].read_count == 1
    assert worksheets["Source Health"].append_rows_count == 1
    assert len(worksheets["Source Health"].rows) == 4


def test_eligible_events_use_source_publish_time_instead_of_recent_analysis_time() -> None:
    recent = (NOW - timedelta(hours=2)).isoformat()
    stale = (NOW - timedelta(days=10)).isoformat()
    headers = [
        "event_id",
        "status",
        "agent_decision",
        "needs_primary_source",
        "analysis_at",
        "published_at",
        "notes",
        "source_ids_json",
        "source_id",
        "canonical_url",
        "section",
        "event_type",
        "event_status",
        "source_tier",
        "source_name",
        "company",
        "confidence",
        "editorial_score",
        "run_id",
    ]
    base = [
        "ELIGIBLE",
        "KEEP",
        "FALSE",
        recent,
        '{"verified_facts":[{"fact_id":"f1","claim":"claim","exact_quote":"quote"}]}',
        '["S001"]',
        "S001",
        "https://borrower.example/1",
        "ai_radar",
        "model_launch",
        "launched",
        "A",
        "Borrower official",
        "Borrower",
        "95",
        "80",
        "collector-run",
    ]
    stale_row = ["event-stale", *base[:4], stale, *base[4:]]
    undated_row = ["event-undated", *base[:4], "", *base[4:]]
    repo, _ = repository(**{"Raw Events": [headers, stale_row, undated_row]})

    eligible = repo.load_eligible_events(NOW - timedelta(hours=96), now=NOW)

    assert [row["event_id"] for row in eligible] == ["event-undated"]


def test_candidate_index_includes_durable_evidence_drops_but_not_failures() -> None:
    repo, _ = repository(
        **{
            "Raw Events": [["event_id", "content_hash", "status"]],
            "Source Health": [
                [
                    "run_id",
                    "failure_stage",
                    "reason_codes",
                    "candidate_id",
                    "notes",
                ],
                [
                    "RUN-1",
                    "evidence_guard",
                    '["EVIDENCE_DROP"]',
                    "drop-1",
                    '{"content_hash":"' + "d" * 64 + '"}',
                ],
                [
                    "RUN-1",
                    "evidence_guard",
                    '["OPENROUTERERROR"]',
                    "retry-1",
                    '{"content_hash":"' + "e" * 64 + '"}',
                ],
                [
                    "RUN-1",
                    "evidence_guard",
                    '["NEEDS_PRIMARY_SOURCE"]',
                    "needs-primary-1",
                    '{"content_hash":"' + "a" * 64 + '"}',
                ],
                [
                    "TEST-1",
                    "evidence_guard",
                    '["EVIDENCE_DROP"]',
                    "test-drop-1",
                    '{"content_hash":"' + "f" * 64 + '"}',
                ],
                [
                    "RUN-2",
                    "evidence_guard",
                    '["EVIDENCE_DROP"]',
                    "drop-v2",
                    '{"content_hash":"'
                    + "b" * 64
                    + '","evidence_policy_version":"intelligence-v2"}',
                ],
            ],
        }
    )

    production_ids, production_hashes = repo.load_candidate_index()
    editorial_ids, editorial_hashes = repo.load_candidate_index(include_needs_primary=False)
    test_ids, test_hashes = repo.load_candidate_index(include_test_only=True)
    current_policy_ids, current_policy_hashes = repo.load_candidate_index(
        evidence_policy_version="intelligence-v2"
    )

    assert production_ids == {"drop-1", "drop-v2", "needs-primary-1"}
    assert production_hashes == {"d" * 64, "b" * 64, "a" * 64}
    assert editorial_ids == {"drop-1", "drop-v2"}
    assert editorial_hashes == {"d" * 64, "b" * 64}
    assert test_ids == {"test-drop-1"}
    assert test_hashes == {"f" * 64}
    assert current_policy_ids == {"drop-v2", "needs-primary-1"}
    assert current_policy_hashes == {"b" * 64, "a" * 64}


def test_duplicate_normalized_headers_fail_closed() -> None:
    repo, _ = repository(**{"Settings": [["Setting", "setting", "value"]]})
    with pytest.raises(SheetSchemaError, match="duplicate"):
        repo.load_settings()
