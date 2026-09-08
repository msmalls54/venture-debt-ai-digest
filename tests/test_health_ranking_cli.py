from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from vdai.cli import (
    _canary_editorial_error,
    _inside_collection_hour,
    _select_canary_sources,
    main,
)
from vdai.config import AppSettings
from vdai.health import evaluate_health_gate
from vdai.models import HealthStatus, SourceConfig, SourceHealth, SourceJob
from vdai.ranking import rank_eligible_events

NOW = datetime(2026, 8, 25, 14, 0, tzinfo=UTC)


def _job(source_id: str, section: str) -> SourceJob:
    source = SourceConfig(
        source_id=source_id,
        section=section,
        organization=source_id,
        source_name=source_id,
        url=f"https://{source_id.lower()}.example.com/news",
        method="html",
        cadence="15 minutes",
        source_tier="A",
        priority="P0",
        allowed_domains=(f"{source_id.lower()}.example.com",),
        primary_source=True,
        discovery_only=False,
        parser_version="html.v2",
    )
    return SourceJob(
        source=source,
        source_job_key=source_id,
        resolved_url=source.url,
        parser_lane="html",
        cadence_minutes=15,
    )


def _receipt(
    job: SourceJob, *, checked_at: datetime = NOW, parser: str = "html.v2"
) -> SourceHealth:
    return SourceHealth(
        source_id=job.source_id,
        source_job_key=job.source_job_key,
        last_attempt_at=checked_at,
        last_success_at=checked_at,
        status=HealthStatus.SUCCESS,
        parser_version=parser,
        updated_at=checked_at,
        run_id="RUN-health",
        source_name=job.source.source_name,
        checked_at=checked_at,
        failure_stage="source_complete",
    )


def test_ranking_suppresses_published_ids_hashes_and_is_stable() -> None:
    events = [
        {
            "event_id": "event-old",
            "candidate_id": "candidate-old",
            "content_hash": "hash-old",
            "editorial_score": 99,
            "published_at": "2026-08-25T13:00:00+00:00",
        },
        {
            "event_id": "event-two",
            "candidate_id": "candidate-two",
            "content_hash": "hash-two",
            "editorial_score": 80,
            "published_at": "2026-08-25T12:00:00+00:00",
        },
        {
            "event_id": "event-three",
            "candidate_id": "candidate-three",
            "content_hash": "hash-three",
            "editorial_score": 80,
            "published_at": "2026-08-25T13:00:00+00:00",
        },
    ]
    history = [{"raw_event_id": "event-old", "content_hash": "hash-old"}]

    ranked = rank_eligible_events(events, history, now=NOW)

    assert [row["event_id"] for row in ranked] == ["event-three", "event-two"]


def test_ranking_suppresses_a_rebuilt_event_with_a_published_canonical_url() -> None:
    events = [
        {
            "event_id": "rebuilt-event-id",
            "candidate_id": "rebuilt-candidate-id",
            "canonical_url": "https://lender.example/deal-one",
            "editorial_score": 99,
            "published_at": "2026-08-25T13:00:00+00:00",
        }
    ]
    history = [{"canonical_url": "https://lender.example/deal-one"}]

    assert rank_eligible_events(events, history, now=NOW) == []


def test_canary_command_writes_terminal_email_and_browser_view(tmp_path, capsys) -> None:
    exit_code = main(["render-canary", "--output-dir", str(tmp_path)])

    output = json.loads(capsys.readouterr().out)
    html = (tmp_path / "digest-preview.html").read_text(encoding="utf-8")
    text = (tmp_path / "digest-preview.txt").read_text(encoding="utf-8")
    dashboard = (tmp_path / "digest-dashboard.html").read_text(encoding="utf-8")
    assert exit_code == 0
    assert output["status"] == "CANARY_RENDERED"
    assert "background:#050706" in html
    assert "linear-gradient(#050706,#050706)" in html
    assert "--black:#050706" in dashboard
    assert "background:#090d0b" in html
    assert "background:#f7f5ee" not in html
    assert "VENTURE DEBT + AI BRIEF" in html
    assert "Signal AI" not in html
    assert "Northstar — $75M credit facility" in html
    assert "3 STORIES" in text
    assert "ANALYST COMMENTARY" in text
    assert "TEST RENDER · EMAIL DELIVERY DISABLED" in html
    assert output["preview_dashboard"].endswith("digest-dashboard.html")
    assert "VENTURE DEBT + AI BRIEF" in dashboard
    assert "Northstar — $75M credit facility" in dashboard
    assert "RUN STATE" not in dashboard
    assert "SOURCES HEALTHY" not in dashboard


def test_disabled_worker_exits_before_credentials_or_network(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GOOGLE_SHEET_ID", "test-google-sheet-id")
    monkeypatch.setenv("WORKER_ENABLED", "false")

    exit_code = main(["run"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output == {
        "delivery_status": "PAUSED",
        "reason_code": "WORKER_DISABLED",
        "status": "PAUSED",
    }


def test_health_gate_rejects_stale_collector_and_parser_mismatch() -> None:
    jobs = [
        _job("S001", "deal_tape"),
        _job("S002", "competitive_field"),
        _job("S003", "ai_radar"),
        _job("S004", "runway_watch"),
    ]
    latest = {job.source_job_key: _receipt(job) for job in jobs}
    latest["S003"] = _receipt(jobs[2], parser="html.v1")

    result = evaluate_health_gate(
        jobs,
        jobs,
        latest,
        threshold=0.9,
        now=NOW,
        collector_completed_at=NOW - timedelta(hours=2),
    )

    assert result.healthy is False
    assert result.collector_fresh is False
    assert "COLLECTOR_STALE_OR_MISSING" in result.reason_codes
    assert "CORE_COVERAGE_BELOW_THRESHOLD" in result.reason_codes
    assert "ai" in result.missing_lanes


def test_dst_guard_accepts_only_seven_am_pacific() -> None:
    settings = AppSettings(google_sheet_id="test-google-sheet-id")

    assert _inside_collection_hour(settings, datetime(2026, 8, 25, 14, 0, tzinfo=UTC))
    assert not _inside_collection_hour(settings, datetime(2026, 8, 25, 15, 0, tzinfo=UTC))
    assert not _inside_collection_hour(settings, datetime(2026, 1, 6, 14, 0, tzinfo=UTC))
    assert _inside_collection_hour(settings, datetime(2026, 1, 6, 15, 0, tzinfo=UTC))


def test_fetch_canary_defaults_to_active_registry_and_can_explicitly_test_inactive() -> None:
    active = _job("S001", "deal_tape").source
    inactive = _job("S002", "ai_radar").source.model_copy(update={"active": False})

    selected = _select_canary_sources([active, inactive], None)
    staged = _select_canary_sources([active, inactive], ["S002"])

    assert [source.source_id for source in selected] == ["S001"]
    assert [source.source_id for source in staged] == ["S002"]
    assert staged[0].active is True


def test_fetch_canary_rejects_transport_green_wrappers() -> None:
    dated = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)

    assert _canary_editorial_error([]) == "EMPTY_SOURCE"
    assert (
        _canary_editorial_error(
            [
                SimpleNamespace(
                    published_at=None,
                    canonical_url="https://example.com/news",
                    parser_lane="rss",
                )
            ]
        )
        == "UNDATED_SOURCE"
    )
    assert (
        _canary_editorial_error(
            [
                SimpleNamespace(
                    published_at=dated,
                    canonical_url="https://example.com/news",
                    parser_lane="rss",
                ),
                SimpleNamespace(
                    published_at=dated,
                    canonical_url="https://example.com/news",
                    parser_lane="rss",
                ),
            ]
        )
        == "COLLAPSED_SOURCE"
    )
    assert (
        _canary_editorial_error(
            [
                SimpleNamespace(
                    published_at=dated,
                    canonical_url="https://example.com/item",
                    parser_lane="rss",
                )
            ]
        )
        == ""
    )
    assert (
        _canary_editorial_error(
            [
                SimpleNamespace(
                    published_at=dated,
                    canonical_url="https://example.com/newsroom/",
                    parser_lane="html",
                )
            ],
            source_url="https://example.com/newsroom/",
        )
        == "COLLAPSED_SOURCE"
    )

    api_items = [
        SimpleNamespace(
            published_at=dated,
            canonical_url="https://example.com/company.json",
            parser_lane="api",
        ),
        SimpleNamespace(
            published_at=dated,
            canonical_url="https://example.com/company.json",
            parser_lane="api",
        ),
    ]
    assert _canary_editorial_error(api_items) == ""
