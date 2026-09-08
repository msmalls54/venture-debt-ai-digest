"""Environment and Google-Sheet configuration normalization."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from itertools import product
from typing import Any
from urllib.parse import urlsplit

from dateutil import parser as date_parser
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from vdai.models import (
    DigestSection,
    ParserLane,
    Priority,
    SourceConfig,
    SourceHealth,
    SourceJob,
    SourceTier,
)


class ConfigurationError(ValueError):
    """Raised when external configuration cannot be made safe and deterministic."""


class AppSettings(BaseSettings):
    """Railway environment configuration.

    Secrets are accepted only through environment variables and remain ``SecretStr`` objects.
    ``service_account_info`` never logs or includes the source JSON in an exception.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    google_sheet_id: str = Field(min_length=10)
    worker_enabled: bool = False
    google_service_account_json: SecretStr = Field(default=SecretStr(""), repr=False)
    openrouter_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    openrouter_model: str = "google/gemini-3.8-flash"
    openrouter_reasoning_effort: str = "high"
    agentmail_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    agentmail_inbox: str = "mikesupdateagent@agentmail.to"
    agentmail_send_enabled: bool = False
    test_mode: bool = True
    timezone: str = "America/Los_Angeles"
    send_hour_local: int = Field(default=7, ge=0, le=23)
    send_minute_window: int = Field(default=60, ge=1, le=60)
    collection_concurrency: int = Field(default=6, ge=1, le=12)
    source_timeout_seconds: int = Field(default=20, ge=5, le=60)
    max_candidates_per_run: int = Field(default=60, ge=1, le=72)
    log_level: str = "INFO"

    @field_validator("agentmail_inbox", mode="before")
    @classmethod
    def normalize_inbox(cls, value: object) -> str:
        return str(value).strip().lower()

    def service_account_info(self) -> dict[str, Any]:
        raw = self.google_service_account_json.get_secret_value()
        if not raw:
            raise ConfigurationError("GOOGLE_SERVICE_ACCOUNT_JSON is required")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ConfigurationError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON") from error
        if not isinstance(value, dict) or value.get("type") != "service_account":
            raise ConfigurationError("Google credential must be a service-account JSON object")
        if not value.get("client_email") or not value.get("private_key"):
            raise ConfigurationError("Google service-account JSON is missing required fields")
        return value


Settings = AppSettings


TRUE_VALUES = frozenset({"true", "1", "yes", "y", "active", "enabled", "approved"})
FALSE_VALUES = frozenset({"false", "0", "no", "n", "inactive", "disabled", "blocked"})


def normalize_key(value: object) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def normalize_bool(value: object, *, default: bool | None = None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or str(value).strip() == "":
        if default is None:
            raise ConfigurationError("blank boolean has no default")
        return default
    normalized = str(value).strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ConfigurationError(f"invalid boolean value: {value!r}")


def normalize_datetime(value: object, *, allow_none: bool = True) -> datetime | None:
    if value is None or str(value).strip() == "":
        if allow_none:
            return None
        raise ConfigurationError("datetime is required")
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = date_parser.isoparse(str(value).strip())
        except (TypeError, ValueError) as error:
            raise ConfigurationError("invalid datetime") from error
    if parsed.tzinfo is None:
        raise ConfigurationError("datetime must include a timezone")
    return parsed.astimezone(UTC)


def normalize_list(value: object, *, lowercase: bool = False) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value]
    else:
        text = str(value).strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    items = [str(item).strip() for item in parsed]
                else:
                    items = re.split(r"[,;|]", text)
            except json.JSONDecodeError:
                items = re.split(r"[,;|]", text)
        else:
            items = re.split(r"[,;|]", text)
    normalized = [item.lower() if lowercase else item for item in items if item]
    return tuple(dict.fromkeys(normalized))


def parse_cadence_minutes(value: object) -> int:
    text = str(value or "daily").strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return max(15, round(float(text)))
    match = re.search(r"\d+(?:\.\d+)?", text)
    amount = float(match.group(0)) if match else 1.0
    if "min" in text:
        minutes = amount
    elif "hour" in text or re.search(r"\bhr", text):
        minutes = amount * 60
    elif "week" in text:
        minutes = amount * 7 * 24 * 60
    elif "month" in text:
        minutes = amount * 30 * 24 * 60
    else:
        minutes = amount * 24 * 60
    return max(15, round(minutes))


def normalize_section(value: object) -> DigestSection:
    key = normalize_key(value)
    aliases = {
        "discovery": DigestSection.DISCOVERY,
        "lead": DigestSection.LEAD,
        "the_lead": DigestSection.LEAD,
        "deal": DigestSection.DEAL_TAPE,
        "deals": DigestSection.DEAL_TAPE,
        "deal_tape": DigestSection.DEAL_TAPE,
        "competitive": DigestSection.COMPETITIVE_FIELD,
        "competitive_field": DigestSection.COMPETITIVE_FIELD,
        "ai": DigestSection.AI_RADAR,
        "ai_radar": DigestSection.AI_RADAR,
        "runway": DigestSection.RUNWAY_WATCH,
        "runway_watch": DigestSection.RUNWAY_WATCH,
        "deposit": DigestSection.DEPOSIT_WATCH,
        "deposit_watch": DigestSection.DEPOSIT_WATCH,
    }
    try:
        return aliases[key]
    except KeyError as error:
        raise ConfigurationError(f"unknown digest section: {value!r}") from error


def normalize_parser_lane(method: object, parser: object = "") -> ParserLane:
    candidates = (normalize_key(parser), normalize_key(method))
    aliases = {
        "rss": ParserLane.RSS_ATOM,
        "atom": ParserLane.RSS_ATOM,
        "feed": ParserLane.RSS_ATOM,
        "rss_atom": ParserLane.RSS_ATOM,
        "api": ParserLane.API,
        "json": ParserLane.API,
        "json_api": ParserLane.API,
        "html": ParserLane.HTML,
        "web_page": ParserLane.HTML,
        "sitemap": ParserLane.SITEMAP_HTML,
        "sitemap_html": ParserLane.SITEMAP_HTML,
        "markdown": ParserLane.MARKDOWN,
        "md": ParserLane.MARKDOWN,
        "html_email": ParserLane.HTML_EMAIL,
        "email": ParserLane.HTML_EMAIL,
    }
    for candidate in candidates:
        if candidate in aliases:
            return aliases[candidate]
    raise ConfigurationError(f"unknown parser lane for method={method!r}, parser={parser!r}")


def normalize_source_tier(
    value: object,
    *,
    primary_hint: bool | None = None,
    discovery_hint: bool | None = None,
) -> SourceTier:
    """Map legacy A-D and the workbook's scored tier labels to evidence tiers."""

    text = str(value or "").strip()
    upper = text.upper()
    if upper in {tier.value for tier in SourceTier}:
        explicit = SourceTier(upper)
        if explicit is SourceTier.D:
            return SourceTier.D
        return SourceTier.A if primary_hint is True else explicit
    key = normalize_key(text)
    if primary_hint is True or "primary" in key or "regulatory" in key:
        return SourceTier.A
    if "official" in key or "corporate" in key or "product" in key:
        return SourceTier.B
    if discovery_hint is True or "discovery" in key:
        return SourceTier.C
    if any(token in key for token in ("social", "unattributed", "ambiguous", "malformed")):
        return SourceTier.D
    raise ConfigurationError(f"unknown source tier label: {value!r}")


def normalize_domain(value: str) -> str:
    text = value.strip().lower()
    text = urlsplit(text).hostname or "" if "://" in text else text.split("/", 1)[0]
    return text.removeprefix("www.").strip(".")


def _pick(row: Mapping[str, Any], *names: str, default: Any = "") -> Any:
    normalized = {normalize_key(key): value for key, value in row.items()}
    for name in names:
        value = normalized.get(normalize_key(name))
        if value is not None and str(value).strip() != "":
            return value
    return default


def source_config_from_row(row: Mapping[str, Any]) -> SourceConfig:
    source_id = str(_pick(row, "source_id", "id")).strip()
    if not source_id:
        raise ConfigurationError("Sources row is missing source_id")
    primary_raw = _pick(row, "primary_source", default="")
    discovery_raw = _pick(row, "discovery_only", default="")
    primary_hint = None if str(primary_raw).strip() == "" else normalize_bool(primary_raw)
    discovery_hint = None if str(discovery_raw).strip() == "" else normalize_bool(discovery_raw)
    tier = normalize_source_tier(
        _pick(row, "source_tier", "tier", default="D"),
        primary_hint=primary_hint,
        discovery_hint=discovery_hint,
    )
    primary = primary_hint if primary_hint is not None else tier in {SourceTier.A, SourceTier.B}
    discovery = discovery_hint if discovery_hint is not None else not primary
    priority_text = str(_pick(row, "priority", default="P2")).strip().upper()
    try:
        priority = Priority(priority_text)
    except ValueError as error:
        raise ConfigurationError(f"invalid priority for {source_id}") from error
    domains = tuple(
        domain
        for domain in (
            normalize_domain(value)
            for value in normalize_list(
                _pick(row, "allowed_domains", "domain_allowlist"), lowercase=True
            )
        )
        if domain
    )
    method = str(_pick(row, "method", "source_method")).strip()
    parser = str(_pick(row, "parser", "parser_lane", default="")).strip()
    lane = normalize_parser_lane(method, parser)
    organization = str(_pick(row, "organization", "company", default=source_id)).strip()
    source_name = str(_pick(row, "source_name", "name", default=organization)).strip()
    return SourceConfig(
        source_id=source_id,
        section=normalize_section(_pick(row, "section", "digest_section")),
        organization=organization,
        source_name=source_name,
        url=str(_pick(row, "url", "feed_url")).strip(),
        method=method,
        # Preserve a configured parser recipe such as ``HTML detail``.  The
        # normalized lane is still derived above (and again during job
        # expansion), while the recipe lets the collector opt into bounded
        # detail-page hydration for listing pages that omit dates.
        parser=parser or lane.value,
        cadence=str(_pick(row, "cadence", default="daily")).strip(),
        source_tier=tier,
        priority=priority,
        active=normalize_bool(_pick(row, "active", "enabled", default=True), default=True),
        region=normalize_list(_pick(row, "region", "jurisdictions")),
        notes=str(_pick(row, "notes", default="")),
        allowed_domains=domains,
        primary_source=primary,
        discovery_only=discovery,
        headers_profile=str(_pick(row, "headers_profile", default="default")),
        max_items=min(int(_pick(row, "max_items", default=25)), 100),
        max_pages=int(_pick(row, "max_pages", default=3)),
        max_bytes=int(_pick(row, "max_bytes", default=1_000_000)),
        parser_version=str(_pick(row, "parser_version", default=f"{lane.value}.v1")),
        logo_domain=str(_pick(row, "logo_domain", default="")),
    )


_CIK_PATTERNS = (
    re.compile(r"(?<=CIK)#{10}", re.IGNORECASE),
    re.compile(r"\{+\s*(?:sec_)?cik\s*\}+", re.IGNORECASE),
)
_COMPANY_PATTERNS = (re.compile(r"\{+\s*(?:companies_house_)?company_number\s*\}+", re.IGNORECASE),)


def watchlist_template_values(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ciks: list[str] = []
    company_numbers: list[str] = []
    for row in rows:
        cik = re.sub(r"\D", "", str(_pick(row, "sec_cik", "cik", default="")))
        if cik:
            ciks.append(cik.zfill(10)[-10:])
        company_number = (
            str(_pick(row, "companies_house_number", "company_number", default="")).strip().upper()
        )
        if company_number:
            company_numbers.append(company_number)
    return tuple(dict.fromkeys(ciks)), tuple(dict.fromkeys(company_numbers))


def _contains_pattern(url: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(url) for pattern in patterns)


def _replace_patterns(url: str, patterns: tuple[re.Pattern[str], ...], value: str) -> str:
    result = url
    for pattern in patterns:
        result = pattern.sub(value, result)
    return result


def expand_source_jobs(
    sources: Iterable[SourceConfig],
    company_watchlist: Iterable[Mapping[str, Any]],
    *,
    manual: bool = False,
) -> list[SourceJob]:
    ciks, company_numbers = watchlist_template_values(company_watchlist)
    jobs: list[SourceJob] = []
    for source in sources:
        if not source.active:
            continue
        wants_cik = _contains_pattern(source.url, _CIK_PATTERNS)
        wants_company = _contains_pattern(source.url, _COMPANY_PATTERNS)
        dimensions: list[tuple[str, tuple[str | None, ...]]] = []
        if wants_cik:
            dimensions.append(("sec_cik", ciks or (None,)))
        if wants_company:
            dimensions.append(("company_number", company_numbers or (None,)))
        combinations = product(*(values for _, values in dimensions)) if dimensions else [()]
        for combination in combinations:
            replacements = dict(zip((name for name, _ in dimensions), combination, strict=True))
            missing = tuple(name for name, value in replacements.items() if value is None)
            url = source.url
            if replacements.get("sec_cik"):
                url = _replace_patterns(url, _CIK_PATTERNS, replacements["sec_cik"] or "")
            if replacements.get("company_number"):
                url = _replace_patterns(
                    url, _COMPANY_PATTERNS, replacements["company_number"] or ""
                )
            suffix = "|".join(
                f"{name}={value}" for name, value in replacements.items() if value is not None
            )
            job_key = f"{source.source_id}:{suffix}" if suffix else source.source_id
            errors = tuple(f"MISSING_TEMPLATE_VALUE_{name.upper()}" for name in missing)
            resolved_url = None if errors else url
            jobs.append(
                SourceJob(
                    source=source,
                    source_job_key=job_key,
                    resolved_url=resolved_url,
                    parser_lane=normalize_parser_lane(source.method, source.parser),
                    cadence_minutes=parse_cadence_minutes(source.cadence),
                    template_values={
                        key: value for key, value in replacements.items() if value is not None
                    },
                    due=True,
                    manual_canary=manual,
                    write_mode="shadow" if manual else "scheduled",
                    configuration_errors=errors,
                )
            )
    return jobs


def expected_source_job_keys(
    sources: Iterable[SourceConfig], company_watchlist: Iterable[Mapping[str, Any]]
) -> tuple[str, ...]:
    return tuple(job.source_job_key for job in expand_source_jobs(sources, company_watchlist))


def latest_health_by_job(health_rows: Iterable[SourceHealth]) -> dict[str, SourceHealth]:
    latest: dict[str, SourceHealth] = {}
    for row in health_rows:
        current = latest.get(row.source_job_key)
        if current is None or row.updated_at > current.updated_at:
            latest[row.source_job_key] = row
    return latest


def source_job_is_due(
    job: SourceJob,
    latest_health: Mapping[str, SourceHealth],
    *,
    now: datetime,
) -> bool:
    if now.tzinfo is None:
        raise ConfigurationError("due-time comparison requires an aware datetime")
    prior = latest_health.get(job.source_job_key)
    if prior is None:
        return True
    if prior.quarantined_until is not None and prior.quarantined_until > now:
        return False
    elapsed_minutes = (now - prior.last_attempt_at).total_seconds() / 60
    return elapsed_minutes >= job.cadence_minutes


def select_due_source_jobs(
    sources: Iterable[SourceConfig],
    company_watchlist: Iterable[Mapping[str, Any]],
    health_rows: Iterable[SourceHealth],
    *,
    now: datetime,
    manual: bool = False,
    manual_source_id: str | None = None,
) -> list[SourceJob]:
    jobs = expand_source_jobs(sources, company_watchlist, manual=manual)
    if manual_source_id:
        jobs = [job for job in jobs if job.source_id == manual_source_id]
    if manual:
        return jobs[:1]
    latest = latest_health_by_job(health_rows)
    return [
        job.model_copy(update={"due": source_job_is_due(job, latest, now=now)})
        for job in jobs
        if source_job_is_due(job, latest, now=now)
    ]


__all__ = [
    "AppSettings",
    "ConfigurationError",
    "Settings",
    "expand_source_jobs",
    "expected_source_job_keys",
    "latest_health_by_job",
    "normalize_bool",
    "normalize_datetime",
    "normalize_key",
    "normalize_list",
    "normalize_parser_lane",
    "normalize_section",
    "normalize_source_tier",
    "parse_cadence_minutes",
    "select_due_source_jobs",
    "source_config_from_row",
    "source_job_is_due",
    "watchlist_template_values",
]
