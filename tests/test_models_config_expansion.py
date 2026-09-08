from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from vdai.config import (
    AppSettings,
    ConfigurationError,
    expand_source_jobs,
    parse_cadence_minutes,
    select_due_source_jobs,
    source_config_from_row,
)
from vdai.models import HealthStatus, SourceHealth

NOW = datetime(2026, 8, 25, 15, 0, tzinfo=UTC)


def source_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "Source ID": "S003",
        "Section": "Deal Tape",
        "Organization": "SEC",
        "Source Name": "SEC submissions",
        "URL": "https://data.sec.gov/submissions/CIK##########.json",
        "Method": "API",
        "Cadence": "Every 6 hours",
        "Source Tier": "A",
        "Priority": "P0",
        "Active": "TRUE",
        "Allowed Domains": "data.sec.gov",
        "Primary Source": "TRUE",
        "Discovery Only": "FALSE",
        "Max Items": "25",
        "Max Pages": "3",
        "Max Bytes": "1000000",
        "Parser Version": "api.v1",
    }
    row.update(overrides)
    return row


def healthy_receipt(
    job_key: str, attempted_at: datetime, *, quarantine: datetime | None = None
) -> SourceHealth:
    return SourceHealth(
        source_id="S003",
        source_job_key=job_key,
        last_attempt_at=attempted_at,
        last_success_at=attempted_at,
        status=HealthStatus.SUCCESS,
        item_count=1,
        consecutive_failures=0,
        quarantined_until=quarantine,
        parser_version="api.v1",
        updated_at=attempted_at,
        run_id="run-old",
        source_name="SEC submissions",
        checked_at=attempted_at,
        failure_stage="source_complete",
        parser_lane="api",
    )


def test_header_aliases_and_boolean_normalization_build_strict_source() -> None:
    source = source_config_from_row(source_row())
    assert source.source_id == "S003"
    assert source.section.value == "deal_tape"
    assert source.active is True
    assert source.primary_source is True
    assert source.discovery_only is False
    assert source.allowed_domains == ("data.sec.gov",)


def test_discovery_only_source_accepts_discovery_section() -> None:
    source = source_config_from_row(
        source_row(
            **{
                "Section": "Discovery",
                "Primary Source": "FALSE",
                "Discovery Only": "TRUE",
            }
        )
    )

    assert source.section.value == "discovery"
    assert source.discovery_only is True


def test_source_item_limit_is_safely_capped() -> None:
    source = source_config_from_row(source_row(**{"Max Items": "250"}))

    assert source.max_items == 100


def test_parser_recipe_is_preserved_for_detail_page_hydration() -> None:
    source = source_config_from_row(
        source_row(**{"Method": "HTML", "Parser": "HTML detail"})
    )

    assert source.parser == "HTML detail"
    jobs = expand_source_jobs([source], [], manual=True)
    assert jobs[0].parser_lane.value == "html"


@pytest.mark.parametrize(
    ("sheet_tier", "primary", "discovery", "expected"),
    [
        ("3-primary", "TRUE", "FALSE", "A"),
        ("3-regulatory", "TRUE", "FALSE", "A"),
        ("2-discovery", "FALSE", "TRUE", "C"),
        ("1-discovery", "FALSE", "TRUE", "C"),
        ("D", "FALSE", "TRUE", "D"),
    ],
)
def test_real_workbook_source_tiers_map_to_evidence_tiers(
    sheet_tier: str, primary: str, discovery: str, expected: str
) -> None:
    source = source_config_from_row(
        source_row(
            **{
                "Source Tier": sheet_tier,
                "Primary Source": primary,
                "Discovery Only": discovery,
            }
        )
    )
    assert source.source_tier.value == expected


def test_watchlist_expansion_uses_current_exact_job_keys() -> None:
    sec = source_config_from_row(source_row())
    companies_house = source_config_from_row(
        source_row(
            **{
                "Source ID": "S004",
                "Organization": "Companies House",
                "Source Name": "Companies House filings",
                "URL": "https://api.company-information.service.gov.uk/company/{company_number}",
                "Allowed Domains": "api.company-information.service.gov.uk",
            }
        )
    )
    jobs = expand_source_jobs(
        [sec, companies_house],
        [
            {"SEC CIK": "320193", "Companies House Number": "ab123456"},
            {"SEC CIK": "789019", "Companies House Number": "ZZ654321"},
        ],
    )
    assert {job.source_job_key for job in jobs} == {
        "S003:sec_cik=0000320193",
        "S003:sec_cik=0000789019",
        "S004:company_number=AB123456",
        "S004:company_number=ZZ654321",
    }
    assert all(job.ready for job in jobs)
    assert all(str(job.resolved_url).startswith("https://") for job in jobs)
    assert {
        str(job.resolved_url) for job in jobs if job.source_id == "S003"
    } == {
        "https://data.sec.gov/submissions/CIK0000320193.json",
        "https://data.sec.gov/submissions/CIK0000789019.json",
    }


def test_missing_template_value_is_fail_closed_not_arbitrary_fetch() -> None:
    jobs = expand_source_jobs([source_config_from_row(source_row())], [])
    assert len(jobs) == 1
    assert jobs[0].source_job_key == "S003"
    assert jobs[0].resolved_url is None
    assert jobs[0].configuration_errors == ("MISSING_TEMPLATE_VALUE_SEC_CIK",)


def test_cadence_due_and_quarantine_rules_are_job_specific() -> None:
    source = source_config_from_row(source_row())
    watchlist = [{"sec_cik": "320193"}, {"sec_cik": "789019"}]
    first_key = "S003:sec_cik=0000320193"
    second_key = "S003:sec_cik=0000789019"
    health = [
        healthy_receipt(first_key, NOW - timedelta(hours=7)),
        healthy_receipt(
            second_key,
            NOW - timedelta(hours=7),
            quarantine=NOW + timedelta(hours=1),
        ),
    ]
    due = select_due_source_jobs([source], watchlist, health, now=NOW)
    assert [job.source_job_key for job in due] == [first_key]
    manual = select_due_source_jobs([source], watchlist, health, now=NOW, manual=True)
    assert len(manual) == 1
    assert manual[0].write_mode == "shadow"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("15 minutes", 15), ("every 6 hours", 360), ("daily", 1440), ("weekly", 10080)],
)
def test_cadence_parser(value: str, expected: int) -> None:
    assert parse_cadence_minutes(value) == expected


def test_service_account_json_is_env_only_and_secret_safe() -> None:
    secret = {
        "type": "service_account",
        "client_email": "worker@example.iam.gserviceaccount.com",
        "private_key": "very-sensitive-private-key",
    }
    settings = AppSettings(
        google_sheet_id="test-google-sheet-id",
        google_service_account_json=json.dumps(secret),
    )
    assert settings.service_account_info()["client_email"].startswith("worker@")
    assert "very-sensitive" not in repr(settings)
    invalid = AppSettings(
        google_sheet_id="test-google-sheet-id",
        google_service_account_json="this secret is invalid JSON",
    )
    with pytest.raises(ConfigurationError) as error:
        invalid.service_account_info()
    assert "this secret" not in str(error.value)


def test_candidate_review_budget_supports_a_broader_bounded_intelligence_pass() -> None:
    settings = AppSettings(
        google_sheet_id="test-google-sheet-id"
    )
    assert settings.max_candidates_per_run == 60
    assert AppSettings(
        google_sheet_id="test-google-sheet-id",
        max_candidates_per_run=72,
    ).max_candidates_per_run == 72
    with pytest.raises(ValueError):
        AppSettings(
            google_sheet_id="test-google-sheet-id",
            max_candidates_per_run=73,
        )
