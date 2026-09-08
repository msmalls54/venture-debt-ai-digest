"""Header-driven Google Sheets repository.

No operation assumes a fixed column number.  Headers are read on every repository call,
formula-like strings are escaped, complex values are deterministic JSON, and retries are
deduplicated by durable business keys.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import zlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from pydantic import ValidationError

from vdai.config import (
    AppSettings,
    ConfigurationError,
    normalize_bool,
    normalize_datetime,
    normalize_key,
    normalize_list,
    normalize_source_tier,
    source_config_from_row,
)
from vdai.models import (
    CandidateV1,
    DigestRun,
    DigestRunStatus,
    Edition,
    EventStatus,
    EventV1,
    EvidenceDecision,
    HealthStatus,
    ParserLane,
    Recipient,
    RecipientBatch,
    SourceConfig,
    SourceHealth,
    Story,
)


class SheetRepositoryError(RuntimeError):
    """Base class for safe, non-secret-bearing repository errors."""


class SheetSchemaError(SheetRepositoryError):
    """Raised when a tab does not satisfy its header contract."""


class SheetSecurityError(SheetRepositoryError):
    """Raised if somebody attempts to place a secret in the Sheet."""


class WorksheetLike(Protocol):
    title: str

    def get_all_values(self) -> list[list[str]]: ...

    def append_row(self, values: Sequence[Any], **kwargs: Any) -> Any: ...

    def append_rows(self, values: Sequence[Sequence[Any]], **kwargs: Any) -> Any: ...

    def update(self, values: Sequence[Sequence[Any]], range_name: str, **kwargs: Any) -> Any: ...


class SpreadsheetLike(Protocol):
    def worksheet(self, title: str) -> WorksheetLike: ...


TAB_SOURCES = "Sources"
TAB_SETTINGS = "Settings"
TAB_COMPANY_WATCHLIST = "Company Watchlist"
TAB_DEPOSIT_WATCH = "Deposit Watch"
TAB_RECIPIENTS = "Recipients"
TAB_RAW_EVENTS = "Raw Events"
TAB_SOURCE_HEALTH = "Source Health"
TAB_PUBLISHED_STORIES = "Published Stories"
TAB_DIGEST_RUNS = "Digest Runs"

_FORMULA_PREFIX = re.compile(r"^[=+\-@]")
_EDITION_COMPRESSION_FORMAT = "vdai-edition+gzip+base64.v1"
_EDITION_COMPRESSION_THRESHOLD = 45_000
_MAX_EDITION_BYTES = 1_000_000
_SECRET_SETTING = re.compile(
    r"(?:^|_)(?:api_?key|token|secret|password|credential|private_?key|service_?account)(?:_|$)",
    re.IGNORECASE,
)
_BOOL_SETTINGS = {
    "test_mode",
    "agentmail_send_enabled",
    "all_sources_enabled",
}
_INT_SETTINGS = {
    "all_sources_target",
    "collector_freshness_minutes",
    "candidate_limit",
    "lead_max_items",
    "deal_tape_max_items",
    "competitive_field_max_items",
    "ai_radar_max_items",
    "runway_watch_max_items",
}
_FLOAT_SETTINGS = {"source_health_threshold"}


def _column_label(index: int) -> str:
    if index < 1:
        raise ValueError("column index must be positive")
    label = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(65 + remainder) + label
    return label


def _formula_safe(value: str) -> str:
    return f"'{value}" if _FORMULA_PREFIX.match(value.lstrip()) else value


def _json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "unicode_string"):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def serialize_sheet_value(value: Any) -> str | int | float | bool:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping | list | tuple | set):
        return json.dumps(_json_safe(value), separators=(",", ":"), sort_keys=True)
    if isinstance(value, str):
        return _formula_safe(value)
    if isinstance(value, int | float):
        return value
    return _formula_safe(str(value))


def _encode_persisted_edition(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep large rendered editions inside the Sheets 50k-character cell limit."""

    normalized = _json_safe(payload)
    raw = json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(raw) <= _EDITION_COMPRESSION_THRESHOLD:
        return dict(normalized)
    compressed = zlib.compress(raw, level=9, wbits=16 + zlib.MAX_WBITS)
    envelope = {
        "_format": _EDITION_COMPRESSION_FORMAT,
        "data": base64.b64encode(compressed).decode("ascii"),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if len(str(serialize_sheet_value(envelope))) >= 50_000:
        raise SheetSchemaError("persisted edition exceeds the Google Sheets cell limit")
    return envelope


def _decode_persisted_edition(payload: Any) -> Any:
    if not isinstance(payload, Mapping) or payload.get("_format") != _EDITION_COMPRESSION_FORMAT:
        return payload
    encoded = payload.get("data")
    expected_hash = payload.get("sha256")
    if not isinstance(encoded, str) or not isinstance(expected_hash, str):
        raise ValueError("compressed edition envelope is incomplete")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = inflater.decompress(compressed, _MAX_EDITION_BYTES + 1)
    except (binascii.Error, zlib.error) as error:
        raise ValueError("compressed edition cannot be decoded") from error
    if len(raw) > _MAX_EDITION_BYTES or not inflater.eof:
        raise ValueError("compressed edition exceeds the safe expansion limit")
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError("compressed edition hash mismatch")
    return json.loads(raw.decode("utf-8"))


def _normalized_table(values: list[list[str]], tab: str) -> tuple[list[str], list[dict[str, str]]]:
    if not values or not values[0]:
        raise SheetSchemaError(f"{tab} has no header row")
    headers = [normalize_key(header) for header in values[0]]
    if any(not header for header in headers):
        raise SheetSchemaError(f"{tab} has a blank header")
    duplicates = sorted({header for header in headers if headers.count(header) > 1})
    if duplicates:
        raise SheetSchemaError(f"{tab} has duplicate normalized headers: {', '.join(duplicates)}")
    rows: list[dict[str, str]] = []
    for raw in values[1:]:
        padded = [*raw, *([""] * max(0, len(headers) - len(raw)))]
        row = dict(zip(headers, padded[: len(headers)], strict=True))
        if any(str(value).strip() for value in row.values()):
            rows.append(row)
    return headers, rows


def _parse_json_list(value: object) -> tuple[str, ...]:
    return normalize_list(value)


def _parse_sheet_time(value: object) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    try:
        return normalize_datetime(value, allow_none=False)
    except ConfigurationError:
        try:
            parsed_date = date.fromisoformat(str(value).strip())
        except ValueError:
            return None
        return datetime.combine(parsed_date, datetime.min.time(), tzinfo=UTC)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _event_from_raw_row(
    row: Mapping[str, Any],
    *,
    raw_facts: Sequence[Mapping[str, Any]],
    source_ids: tuple[str, ...],
    observed_at: datetime,
) -> EventV1:
    """Reconstruct the strict event contract from the denormalized Raw Events tab."""

    source_id = str(row.get("source_id") or source_ids[0]).strip()
    evidence_tier = normalize_source_tier(row.get("evidence_tier") or row.get("source_tier") or "D")
    canonical_url = str(row.get("canonical_url") or "").strip()
    facts: list[dict[str, Any]] = []
    for raw_fact in raw_facts:
        exact_quote = str(raw_fact.get("exact_quote") or row.get("evidence_snippet") or "").strip()
        claim = str(raw_fact.get("claim") or exact_quote).strip()
        fact_id = str(raw_fact.get("fact_id") or "").strip()
        if not fact_id or not claim or not exact_quote:
            continue
        facts.append(
            {
                "fact_id": fact_id,
                "claim": claim,
                "exact_quote": exact_quote,
                "source_id": str(raw_fact.get("source_id") or source_id).strip(),
                "source_url": str(raw_fact.get("source_url") or canonical_url).strip(),
                "evidence_tier": raw_fact.get("evidence_tier") or evidence_tier,
                "field": _optional_text(raw_fact.get("field")),
                "value": raw_fact.get("value"),
            }
        )
    notes: dict[str, Any] = {}
    try:
        parsed_notes = json.loads(str(row.get("notes") or "{}"))
        if isinstance(parsed_notes, dict):
            notes = parsed_notes
    except json.JSONDecodeError:
        pass
    event_status = str(
        row.get("event_status") or notes.get("model_event_status") or EventStatus.UNKNOWN
    ).strip()
    try:
        event_status = EventStatus(event_status).value
    except ValueError:
        event_status = EventStatus.UNKNOWN.value
    decision = str(row.get("agent_decision") or EvidenceDecision.KEEP).strip().upper()
    if decision == "ACCEPT":
        decision = EvidenceDecision.KEEP.value
    return EventV1(
        event_id=str(row.get("event_id") or row.get("raw_event_id") or "").strip(),
        candidate_id=str(row.get("candidate_id") or row.get("event_id") or "").strip(),
        run_id=str(row.get("run_id") or f"legacy-{row.get('event_id', '')}").strip(),
        source_id=source_id,
        source_job_key=str(row.get("source_job_key") or source_id).strip(),
        decision=decision,
        section=str(row.get("section") or "").strip(),
        event_type=str(row.get("event_type") or "").strip(),
        event_status=event_status,
        company=_optional_text(row.get("company")),
        borrower=_optional_text(row.get("borrower")),
        lenders=_parse_json_list(row.get("lenders")),
        counterparty=_optional_text(row.get("counterparty")),
        total_commitment=_optional_text(row.get("amount_headline")),
        initial_funding=_optional_text(row.get("amount_funded")),
        currency=_optional_text(row.get("currency")),
        facility_type=_optional_text(row.get("facility_type")),
        maturity=_optional_text(row.get("maturity")),
        rate=_optional_text(row.get("rate")),
        security=_optional_text(row.get("security")),
        sector=_optional_text(row.get("sector")),
        geography=_parse_json_list(row.get("geography")),
        acquisition_stage=_optional_text(row.get("acquisition_stage")),
        partner_banks=_parse_json_list(row.get("partner_bank")),
        cash_vehicle=_optional_text(row.get("cash_vehicle")),
        evidence_tier=evidence_tier,
        facts=tuple(facts),
        source_ids=source_ids,
        corroborating_urls=_parse_json_list(row.get("corroborating_urls_json")),
        confidence=int(row.get("confidence") or 0),
        editorial_score=int(row.get("editorial_score") or 0),
        reasoning=str(notes.get("reasoning") or "")[:1_500],
        analyzed_at=_parse_sheet_time(row.get("analysis_at")) or observed_at,
    )


class GoogleSheetsRepository:
    """Small, injectable gspread repository for the canonical workbook."""

    def __init__(self, spreadsheet: SpreadsheetLike):
        self._spreadsheet = spreadsheet

    @classmethod
    def from_settings(cls, settings: AppSettings) -> GoogleSheetsRepository:
        # Imports are deliberately local so importing this module never initializes credentials.
        import gspread

        client = gspread.service_account_from_dict(settings.service_account_info())
        return cls(client.open_by_key(settings.google_sheet_id))

    def _worksheet(self, tab: str) -> WorksheetLike:
        try:
            return self._spreadsheet.worksheet(tab)
        except Exception as error:  # gspread-specific exceptions stay behind this boundary
            raise SheetRepositoryError(f"required tab is unavailable: {tab}") from error

    def _read(self, tab: str) -> tuple[list[str], list[dict[str, str]]]:
        worksheet = self._worksheet(tab)
        try:
            return _normalized_table(worksheet.get_all_values(), tab)
        except SheetRepositoryError:
            raise
        except Exception as error:
            raise SheetRepositoryError(f"could not read tab: {tab}") from error

    def load_sources(self, *, active_only: bool = True) -> list[SourceConfig]:
        _, rows = self._read(TAB_SOURCES)
        sources: list[SourceConfig] = []
        seen: set[str] = set()
        for index, row in enumerate(rows, start=2):
            # The live Sheet carries FALSE defaults in two checkbox columns far
            # below the populated registry. Ignore those formatting-only rows,
            # while still rejecting any partially populated source definition.
            if not any(
                str(value).strip()
                for key, value in row.items()
                if normalize_key(key) not in {"primary_source", "discovery_only"}
            ):
                continue
            try:
                source = source_config_from_row(row)
            except (ConfigurationError, ValidationError, ValueError) as error:
                raise SheetSchemaError(f"Sources row {index} is invalid: {error}") from error
            if source.source_id in seen:
                raise SheetSchemaError(f"Sources contains duplicate source_id: {source.source_id}")
            seen.add(source.source_id)
            if source.active or not active_only:
                sources.append(source)
        return sources

    def load_settings(self) -> dict[str, Any]:
        _, rows = self._read(TAB_SETTINGS)
        settings: dict[str, Any] = {}
        for index, row in enumerate(rows, start=2):
            key = normalize_key(row.get("setting") or row.get("setting_key") or row.get("key"))
            if not key:
                continue
            if key in settings:
                raise SheetSchemaError(f"Settings contains duplicate key: {key}")
            value: Any = row.get("value", row.get("setting_value", ""))
            if _SECRET_SETTING.search(key) and str(value).strip():
                raise SheetSecurityError(
                    f"Settings row {index} attempts to store a secret; use Railway variables"
                )
            if key in _BOOL_SETTINGS:
                value = normalize_bool(value, default=False)
            elif key in _INT_SETTINGS and str(value).strip():
                value = int(value)
            elif key in _FLOAT_SETTINGS and str(value).strip():
                value = float(value)
            elif key.endswith("_at") and str(value).strip():
                value = normalize_datetime(value, allow_none=False)
            settings[key] = value
        return settings

    @staticmethod
    def _normalize_watchlist_row(row: Mapping[str, Any]) -> dict[str, Any]:
        normalized = {normalize_key(key): value for key, value in row.items()}
        aliases = {
            "company_name": "company",
            "name": "company",
            "cik": "sec_cik",
            "companies_house_no": "companies_house_number",
            "company_number": "companies_house_number",
            "enabled": "active",
        }
        for alias, canonical in aliases.items():
            if canonical not in normalized and alias in normalized:
                normalized[canonical] = normalized[alias]
        for key in list(normalized):
            value = normalized[key]
            if key in {"active", "enabled", "watch", "deposit_competitor"} and str(value).strip():
                normalized[key] = normalize_bool(value, default=False)
            elif (key.endswith("_at") or key.endswith("_date")) and str(value).strip():
                normalized[key] = normalize_datetime(value, allow_none=False)
            elif key in {"aliases", "allowed_domains", "jurisdictions"}:
                normalized[key] = normalize_list(value)
        return normalized

    def load_company_watchlist(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        _, rows = self._read(TAB_COMPANY_WATCHLIST)
        normalized = [self._normalize_watchlist_row(row) for row in rows]
        return [
            row
            for row in normalized
            if not active_only or row.get("active", True) is not False
        ]

    def load_deposit_watch(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        _, rows = self._read(TAB_DEPOSIT_WATCH)
        normalized = [self._normalize_watchlist_row(row) for row in rows]
        return [
            row
            for row in normalized
            if not active_only or row.get("active", True) is not False
        ]

    def load_recipients(
        self,
        *,
        exclude_addresses: Iterable[str] = (),
        max_recipients: int = 49,
    ) -> RecipientBatch:
        if not 1 <= max_recipients <= 49:
            raise ValueError("max_recipients must be between 1 and 49")
        _, rows = self._read(TAB_RECIPIENTS)
        excluded = {address.strip().lower() for address in exclude_addresses}
        recipients: list[Recipient] = []
        seen: set[str] = set()
        invalid_count = duplicate_count = excluded_count = overflow_count = 0
        for row in rows:
            email = str(row.get("email") or row.get("email_address") or "").strip().lower()
            if not email:
                continue
            active_value = str(row.get("active") or "").strip()
            if active_value and not normalize_bool(active_value, default=False):
                excluded_count += 1
                continue
            if email in excluded:
                excluded_count += 1
                continue
            if email in seen:
                duplicate_count += 1
                continue
            try:
                recipient = Recipient(
                    recipient_id=str(row.get("recipient_id") or row.get("id") or "").strip()
                    or None,
                    email=email,
                    display_name=str(row.get("display_name") or row.get("name") or "").strip()
                    or None,
                )
            except ValidationError:
                invalid_count += 1
                continue
            seen.add(email)
            if len(recipients) >= max_recipients:
                overflow_count += 1
                continue
            recipients.append(recipient)
        return RecipientBatch(
            recipients=tuple(recipients),
            invalid_count=invalid_count,
            duplicate_count=duplicate_count,
            excluded_count=excluded_count,
            overflow_count=overflow_count,
        )

    def load_source_health(
        self,
        *,
        run_prefixes: tuple[str, ...] | None = None,
    ) -> list[SourceHealth]:
        """Load strict health receipts, optionally scoped to one runtime namespace.

        Prefix filtering intentionally happens before strict row parsing. The shared Sheet
        contains preserved n8n-era statuses and failure stages that are not part of the
        Railway contract; those rows must remain visible in the workbook without being
        interpreted as Railway health.
        """

        _, rows = self._read(TAB_SOURCE_HEALTH)
        output: list[SourceHealth] = []
        for index, row in enumerate(rows, start=2):
            run_id = str(row.get("run_id") or "").strip()
            if run_prefixes is not None and not any(
                run_id.startswith(prefix) for prefix in run_prefixes
            ):
                continue
            try:
                status = HealthStatus(str(row.get("status") or row.get("health_status")).upper())
                lane_value = str(row.get("parser_lane") or "").strip()
                output.append(
                    SourceHealth(
                        source_id=row.get("source_id", ""),
                        source_job_key=row.get("source_job_key") or row.get("source_id", ""),
                        last_attempt_at=normalize_datetime(
                            row.get("last_attempt_at") or row.get("checked_at"), allow_none=False
                        ),
                        last_success_at=normalize_datetime(row.get("last_success_at")),
                        status=status,
                        http_status=int(row["http_status"]) if row.get("http_status") else None,
                        item_count=int(row.get("item_count") or 0),
                        etag=row.get("etag", ""),
                        last_modified=row.get("last_modified", ""),
                        consecutive_failures=int(row.get("consecutive_failures") or 0),
                        quarantined_until=normalize_datetime(row.get("quarantined_until")),
                        parser_version=row.get("parser_version") or "unknown.v1",
                        last_error=row.get("last_error", ""),
                        updated_at=normalize_datetime(
                            row.get("updated_at") or row.get("checked_at"), allow_none=False
                        ),
                        run_id=run_id or f"legacy-health-{index}",
                        source_name=row.get("source_name") or row.get("source_id") or "unknown",
                        checked_at=normalize_datetime(
                            row.get("checked_at") or row.get("last_attempt_at"), allow_none=False
                        ),
                        health_status=status,
                        failure_stage=row.get("failure_stage") or "source_guard",
                        reason_codes=_parse_json_list(row.get("reason_codes")),
                        candidate_id=row.get("candidate_id", ""),
                        canonical_url=row.get("canonical_url") or None,
                        parser_lane=ParserLane(lane_value) if lane_value else None,
                        notes=row.get("notes", ""),
                    )
                )
            except (ConfigurationError, ValidationError, ValueError) as error:
                raise SheetSchemaError(f"Source Health row {index} is invalid: {error}") from error
        return output

    def load_last_sent_at(self) -> datetime | None:
        """Return the newest durable provider-accepted send timestamp."""

        _, rows = self._read(TAB_DIGEST_RUNS)
        sent_times: list[datetime] = []
        for row in rows:
            if str(row.get("status") or "").strip().upper() != "SENT":
                continue
            sent_at = _parse_sheet_time(
                row.get("accepted_at")
                or row.get("completed_at")
                or row.get("started_at")
                or row.get("digest_date")
            )
            if sent_at is not None:
                sent_times.append(sent_at)
        return max(sent_times, default=None)

    def raw_event_exists(
        self,
        *,
        event_id: str | None = None,
        candidate_id: str | None = None,
        content_hash: str | None = None,
    ) -> bool:
        """Check durable event identity without relying on a fixed column position."""

        lookups = {
            "event_id": (event_id or "").strip(),
            "candidate_id": (candidate_id or "").strip(),
            "content_hash": (content_hash or "").strip(),
        }
        active = {key: value for key, value in lookups.items() if value}
        if not active:
            raise ValueError("at least one durable event key is required")
        _, rows = self._read(TAB_RAW_EVENTS)
        return any(
            any(str(row.get(key, "")).strip() == value for key, value in active.items())
            for row in rows
        )

    def candidate_exists(
        self,
        candidate: CandidateV1 | str,
        *,
        content_hash: str | None = None,
    ) -> bool:
        """Return durable existence for a CandidateV1 or a candidate ID string."""

        if isinstance(candidate, CandidateV1):
            candidate_id = candidate.candidate_id
            content_hash = candidate.content_sha256
        else:
            candidate_id = str(candidate).strip()
        return self.raw_event_exists(
            event_id=candidate_id,
            candidate_id=candidate_id,
            content_hash=content_hash,
        )

    def load_eligible_events(
        self,
        since: datetime | None = None,
        *,
        hours: int = 30,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Load recent evidence-ready rows and reconstruct source-backed facts.

        The returned mapping contains editorial evidence only and never recipient data.
        Legacy rows without reconstructable facts are excluded fail-closed.
        """

        if not 1 <= hours <= 168:
            raise ValueError("hours must be between 1 and 168")
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if since is not None and since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        cutoff = (
            since.astimezone(UTC).timestamp()
            if since is not None
            else now.astimezone(UTC).timestamp() - hours * 60 * 60
        )
        _, rows = self._read(TAB_RAW_EVENTS)
        eligible: list[dict[str, Any]] = []
        for row in rows:
            status = str(row.get("status") or "").strip().upper()
            decision = str(row.get("agent_decision") or "").strip().upper()
            if status != "ELIGIBLE" or decision not in {"KEEP", "ACCEPT"}:
                continue
            try:
                if normalize_bool(row.get("needs_primary_source"), default=False):
                    continue
            except ConfigurationError:
                continue
            source_published = _parse_sheet_time(row.get("published_at"))
            observed = _parse_sheet_time(
                row.get("analysis_at") or row.get("first_seen_at") or row.get("published_at")
            )
            freshness_time = source_published or observed
            if observed is None or freshness_time is None or freshness_time.timestamp() < cutoff:
                continue
            notes: dict[str, Any] = {}
            note_text = str(row.get("notes") or "").strip()
            if note_text:
                try:
                    parsed = json.loads(note_text)
                    if isinstance(parsed, dict):
                        notes = parsed
                except json.JSONDecodeError:
                    pass
            if str(notes.get("test_run_id") or "").startswith("TEST-VD-AI-"):
                continue
            facts = (
                notes.get("verified_facts") if isinstance(notes.get("verified_facts"), list) else []
            )
            facts = [fact for fact in facts if isinstance(fact, dict) and fact.get("fact_id")]
            if not facts:
                fact_ids = _parse_json_list(row.get("fact_ids_json"))
                snippet = str(row.get("evidence_snippet") or "").strip()
                if fact_ids and snippet:
                    facts = [
                        {
                            "fact_id": fact_id,
                            "claim": snippet,
                            "exact_quote": snippet,
                            "source_url": row.get("canonical_url", ""),
                        }
                        for fact_id in fact_ids
                    ]
            source_ids = _parse_json_list(row.get("source_ids_json"))
            if not source_ids and row.get("source_id"):
                source_ids = (str(row["source_id"]).strip(),)
            if not facts or not source_ids or not row.get("canonical_url"):
                continue
            try:
                event = _event_from_raw_row(
                    row,
                    raw_facts=facts,
                    source_ids=source_ids,
                    observed_at=observed,
                )
            except (TypeError, ValueError, ValidationError):
                continue
            event_row = event.model_dump(mode="python")
            event_row.update(
                {
                    "source_name": str(row.get("source_name") or "").strip(),
                    "content_hash": str(row.get("content_hash") or "").strip(),
                    "canonical_url": str(row.get("canonical_url") or "").strip(),
                    "observed_at": observed,
                    "published_at": source_published,
                }
            )
            eligible.append(event_row)
        eligible.sort(key=lambda row: row["observed_at"], reverse=True)
        return eligible

    def load_published_history(
        self,
        since: datetime | None = None,
        *,
        days: int = 7,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= days <= 90:
            raise ValueError("days must be between 1 and 90")
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if since is not None and since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        cutoff = (
            since.astimezone(UTC).timestamp()
            if since is not None
            else now.astimezone(UTC).timestamp() - days * 24 * 60 * 60
        )
        _, rows = self._read(TAB_PUBLISHED_STORIES)
        history: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("status") or "").strip().upper() not in {"SENT", "PUBLISHED"}:
                continue
            published = _parse_sheet_time(
                row.get("included_at")
                or row.get("selected_at")
                or row.get("published_at")
                or row.get("digest_date")
            )
            if published is None or published.timestamp() < cutoff:
                continue
            history.append({**row, "history_at": published})
        history.sort(key=lambda row: row["history_at"], reverse=True)
        return history

    def digest_run_exists(
        self,
        send_id: str,
        *,
        statuses: Iterable[DigestRunStatus | str] = (),
    ) -> bool:
        wanted = {
            status.value if isinstance(status, DigestRunStatus) else str(status).strip().upper()
            for status in statuses
        }
        _, rows = self._read(TAB_DIGEST_RUNS)
        return any(
            str(row.get("send_id") or "").strip() == send_id
            and (not wanted or str(row.get("status") or "").strip().upper() in wanted)
            for row in rows
        )

    def sent_digest_exists(self, send_id: str) -> bool:
        return self.digest_run_exists(send_id, statuses=(DigestRunStatus.SENT,))

    def digest_was_sent(self, send_id: str) -> bool:
        """Public idempotency lookup used by the orchestrator before delivery."""

        return self.sent_digest_exists(send_id)

    def load_ready_edition(self, send_id: str) -> Edition | None:
        """Return the exact persisted READY edition for idempotent delivery retries."""

        _, rows = self._read(TAB_DIGEST_RUNS)
        editions: list[Edition] = []
        payload_fingerprints: set[str] = set()
        for row in rows:
            if str(row.get("send_id") or "").strip() != send_id:
                continue
            if str(row.get("status") or "").strip().upper() != "READY_TO_SEND":
                continue
            raw_payload = str(row.get("edition_json") or "").strip()
            if not raw_payload:
                raise SheetSchemaError("READY_TO_SEND digest is missing its persisted edition")
            try:
                payload = _decode_persisted_edition(json.loads(raw_payload))
                edition = Edition.model_validate(payload)
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
                raise SheetSchemaError(
                    "READY_TO_SEND digest has an invalid persisted edition"
                ) from error
            if not edition.html or not edition.text:
                raise SheetSchemaError("READY_TO_SEND digest is missing a rendered body")
            editions.append(edition)
            payload_fingerprints.add(
                json.dumps(edition.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            )
        if len(payload_fingerprints) > 1:
            raise SheetSecurityError("conflicting READY_TO_SEND editions share one send_id")
        return editions[-1] if editions else None

    def load_latest_persisted_edition(self) -> Edition | None:
        """Return the newest saved READY or SENT edition for model-free rerendering."""

        _, rows = self._read(TAB_DIGEST_RUNS)
        candidates: list[tuple[float, int, Mapping[str, Any]]] = []
        for index, row in enumerate(rows):
            if str(row.get("status") or "").strip().upper() not in {
                "READY_TO_SEND",
                "SENT",
            }:
                continue
            raw_payload = str(row.get("edition_json") or "").strip()
            if not raw_payload:
                continue
            observed = _parse_sheet_time(
                row.get("completed_at") or row.get("started_at") or row.get("digest_date")
            )
            candidates.append((observed.timestamp() if observed else 0.0, index, row))
        if not candidates:
            return None

        row = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]
        try:
            payload = _decode_persisted_edition(json.loads(str(row["edition_json"])))
            edition = Edition.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
            raise SheetSchemaError("latest digest has an invalid persisted edition") from error
        if not edition.stories:
            raise SheetSchemaError("latest digest has no persisted stories to rerender")
        return edition

    def load_candidate_index(
        self,
        *,
        include_test_only: bool = False,
        include_needs_primary: bool = True,
        evidence_policy_version: str | None = None,
    ) -> tuple[set[str], set[str]]:
        """Read candidate IDs and hashes once for bounded in-memory deduplication.

        ``NEEDS_PRIMARY_SOURCE`` is retryable on an editorial/send run because a direct
        search result or a better evidence policy may now make the item publishable.
        Ordinary collection runs keep it in the index so they do not repeatedly spend
        model budget on the same unresolved article. When an evidence policy version is
        supplied, DROP receipts from older policies are deliberately excluded so they can
        receive one review under the current policy.
        """

        _, rows = self._read(TAB_RAW_EVENTS)
        candidate_ids: set[str] = set()
        content_hashes: set[str] = set()
        for row in rows:
            status = str(row.get("status") or "").strip().upper()
            notes: dict[str, Any] = {}
            try:
                parsed_notes = json.loads(str(row.get("notes") or "{}"))
                if isinstance(parsed_notes, dict):
                    notes = parsed_notes
            except json.JSONDecodeError:
                pass
            marked_test = status == "TEST_ONLY" or str(notes.get("test_run_id") or "").startswith(
                "TEST-"
            )
            if marked_test and not include_test_only:
                continue
            if not marked_test and include_test_only:
                continue
            for key in ("candidate_id", "event_id", "raw_event_id"):
                value = str(row.get(key) or "").strip()
                if value:
                    candidate_ids.add(value)
            content_hash = str(row.get("content_hash") or "").strip()
            if content_hash:
                content_hashes.add(content_hash)

        # Validated evidence drops are durable review outcomes. Reading them back keeps
        # unchanged undated pages from consuming the same model budget every morning.
        _, health_rows = self._read(TAB_SOURCE_HEALTH)
        for row in health_rows:
            run_id = str(row.get("run_id") or "").strip()
            marked_test = run_id.startswith("TEST-")
            if marked_test and not include_test_only:
                continue
            if not marked_test and include_test_only:
                continue
            if str(row.get("failure_stage") or "").strip() != "evidence_guard":
                continue
            reasons = set(_parse_json_list(row.get("reason_codes")))
            if not reasons.intersection({"EVIDENCE_DROP", "NEEDS_PRIMARY_SOURCE"}):
                continue
            if not include_needs_primary and "NEEDS_PRIMARY_SOURCE" in reasons:
                continue
            try:
                notes = json.loads(str(row.get("notes") or "{}"))
            except json.JSONDecodeError:
                notes = {}
            if not isinstance(notes, dict):
                notes = {}
            if (
                evidence_policy_version is not None
                and "EVIDENCE_DROP" in reasons
                and str(notes.get("evidence_policy_version") or "")
                != evidence_policy_version
            ):
                continue
            candidate_id = str(row.get("candidate_id") or "").strip()
            if candidate_id:
                candidate_ids.add(candidate_id)
            content_hash = str(notes.get("content_hash") or "").strip()
            if content_hash:
                content_hashes.add(content_hash)
        return candidate_ids, content_hashes

    def load_latest_health_by_job(
        self,
        current_job_keys: Iterable[str] | None = None,
        *,
        run_prefixes: tuple[str, ...] | None = None,
    ) -> dict[str, SourceHealth]:
        wanted = set(current_job_keys) if current_job_keys is not None else None
        latest: dict[str, SourceHealth] = {}
        for row in self.load_source_health(run_prefixes=run_prefixes):
            if wanted is not None and row.source_job_key not in wanted:
                continue
            current = latest.get(row.source_job_key)
            if current is None or row.updated_at > current.updated_at:
                latest[row.source_job_key] = row
        return latest

    def _upsert_row(
        self,
        tab: str,
        row: Mapping[str, Any],
        *,
        key_fields: tuple[str, ...],
        allow_blank_key_fields: tuple[str, ...] = (),
    ) -> tuple[str, int]:
        return self._upsert_rows(
            tab,
            [row],
            key_fields=key_fields,
            allow_blank_key_fields=allow_blank_key_fields,
        )[0]

    def _upsert_rows(
        self,
        tab: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        key_fields: tuple[str, ...],
        allow_blank_key_fields: tuple[str, ...] = (),
    ) -> list[tuple[str, int]]:
        if not rows:
            return []
        worksheet = self._worksheet(tab)
        headers, existing = _normalized_table(worksheet.get_all_values(), tab)
        missing_keys = [key for key in key_fields if key not in headers]
        if missing_keys:
            raise SheetSchemaError(
                f"{tab} is missing durable-key headers: {', '.join(missing_keys)}"
            )
        existing_keys: dict[tuple[str, ...], int] = {}
        for index, existing_row in enumerate(existing, start=2):
            key = tuple(str(existing_row.get(field, "")) for field in key_fields)
            existing_keys.setdefault(key, index)

        seen_input_keys: set[tuple[str, ...]] = set()
        updates: list[tuple[int, list[Any]]] = []
        appends: list[list[Any]] = []
        results: list[tuple[str, int]] = []
        for row in rows:
            normalized_row = {normalize_key(key): value for key, value in row.items()}
            missing_values = [
                key
                for key in key_fields
                if key not in allow_blank_key_fields
                and str(normalized_row.get(key, "")).strip() == ""
            ]
            if missing_values:
                raise SheetSchemaError(
                    f"{tab} row is missing durable-key values: {', '.join(missing_values)}"
                )
            serialized = {
                key: serialize_sheet_value(value) for key, value in normalized_row.items()
            }
            durable_key = tuple(str(serialized.get(key, "")) for key in key_fields)
            if durable_key in seen_input_keys:
                raise SheetSchemaError(f"{tab} batch contains a duplicate durable key")
            seen_input_keys.add(durable_key)
            values = [serialized.get(header, "") for header in headers]
            target_index = existing_keys.get(durable_key)
            if target_index is None:
                target_index = len(existing) + len(appends) + 2
                appends.append(values)
                results.append(("appended", target_index))
            else:
                updates.append((target_index, values))
                results.append(("updated", target_index))
        try:
            last_column = _column_label(len(headers))
            for target_index, values in updates:
                worksheet.update(
                    values=[values],
                    range_name=f"A{target_index}:{last_column}{target_index}",
                    value_input_option="RAW",
                )
            if appends:
                append_rows = getattr(worksheet, "append_rows", None)
                if callable(append_rows):
                    append_rows(appends, value_input_option="RAW")
                else:
                    for values in appends:
                        worksheet.append_row(values, value_input_option="RAW")
            return results
        except Exception as error:
            raise SheetRepositoryError(f"could not write tab: {tab}") from error

    @staticmethod
    def _raw_event_row(
        event: EventV1, candidate: CandidateV1, *, status: str = "ELIGIBLE"
    ) -> dict[str, Any]:
        if event.decision is not EvidenceDecision.KEEP:
            raise SheetRepositoryError("only KEEP events can be written to Raw Events")
        if status not in {"ELIGIBLE", "TEST_ONLY"}:
            raise SheetRepositoryError("Raw Events status must be ELIGIBLE or TEST_ONLY")
        first_fact = event.facts[0] if event.facts else None
        notes: dict[str, Any] = {
            "model_event_status": event.event_status,
            "verified_facts": event.facts,
            "reasoning": event.reasoning,
        }
        if status == "TEST_ONLY":
            notes["test_run_id"] = event.run_id
        return {
            "event_id": event.event_id,
            "first_seen_at": candidate.retrieved_at,
            "published_at": candidate.published_at,
            "source_id": candidate.source_id,
            "source_tier": candidate.source_class,
            "source_name": candidate.source_name,
            "canonical_url": candidate.canonical_url,
            "discovery_url": candidate.raw_url
            if candidate.raw_url != candidate.canonical_url
            else "",
            "external_id": candidate.item_id,
            "section": event.section,
            "event_type": event.event_type,
            "company": event.company or event.borrower or "",
            "borrower": event.borrower or "",
            "lenders": event.lenders,
            "counterparty": event.counterparty or "",
            "amount_headline": event.total_commitment,
            "amount_funded": event.initial_funding,
            "currency": event.currency or "",
            "facility_type": event.facility_type or "",
            "maturity": event.maturity,
            "rate": event.rate or "",
            "security": event.security or "",
            "sector": event.sector or "",
            "geography": event.geography,
            "acquisition_stage": event.acquisition_stage or "",
            "partner_bank": event.partner_banks,
            "cash_vehicle": event.cash_vehicle or "",
            "confidence": event.confidence,
            "evidence_snippet": first_fact.exact_quote if first_fact else "",
            "content_hash": candidate.content_sha256,
            "needs_primary_source": candidate.discovery_only or not candidate.primary_source,
            "status": status,
            "reviewed": False,
            "notes": notes,
            "source_title": candidate.title,
            "event_status": event.event_status,
            "evidence_tier": event.evidence_tier,
            "fact_ids_json": tuple(fact.fact_id for fact in event.facts),
            "source_ids_json": event.source_ids,
            "corroborating_urls_json": event.corroborating_urls,
            "editorial_score": event.editorial_score,
            "agent_decision": event.decision,
            "analysis_at": event.analyzed_at,
            "candidate_id": candidate.candidate_id,
            "raw_event_id": event.event_id,
            "run_id": event.run_id,
            "source_job_key": event.source_job_key,
        }

    def upsert_raw_event(
        self, event: EventV1, candidate: CandidateV1, *, status: str = "ELIGIBLE"
    ) -> tuple[str, int]:
        if event.candidate_id != candidate.candidate_id:
            raise SheetRepositoryError("event and candidate IDs do not match")
        return self._upsert_row(
            TAB_RAW_EVENTS,
            self._raw_event_row(event, candidate, status=status),
            key_fields=("event_id",),
        )

    def upsert_raw_events(
        self,
        items: Sequence[tuple[EventV1, CandidateV1]],
        *,
        status: str = "ELIGIBLE",
    ) -> list[tuple[str, int]]:
        rows: list[dict[str, Any]] = []
        for event, candidate in items:
            if event.candidate_id != candidate.candidate_id:
                raise SheetRepositoryError("event and candidate IDs do not match")
            rows.append(self._raw_event_row(event, candidate, status=status))
        return self._upsert_rows(TAB_RAW_EVENTS, rows, key_fields=("event_id",))

    def append_source_health(self, health: SourceHealth) -> tuple[str, int]:
        row = health.model_dump(mode="python")
        return self._upsert_row(
            TAB_SOURCE_HEALTH,
            row,
            key_fields=("run_id", "source_job_key", "failure_stage", "candidate_id", "status"),
            allow_blank_key_fields=("candidate_id",),
        )

    def append_source_health_many(self, receipts: Sequence[SourceHealth]) -> list[tuple[str, int]]:
        return self._upsert_rows(
            TAB_SOURCE_HEALTH,
            [health.model_dump(mode="python") for health in receipts],
            key_fields=("run_id", "source_job_key", "failure_stage", "candidate_id", "status"),
            allow_blank_key_fields=("candidate_id",),
        )

    @staticmethod
    def _published_story_row(
        story: Story,
        edition: Edition,
        *,
        send_id: str,
        recipient_group: str,
        status: DigestRunStatus,
        included_at: datetime,
    ) -> dict[str, Any]:
        citation = story.citations[0]
        return {
            "digest_date": edition.edition_date,
            "send_id": send_id,
            "section": story.section,
            "rank": story.rank,
            "headline": story.headline,
            "company": story.company,
            "summary": story.dek,
            "why_it_matters": story.why_it_matters,
            "amount": story.amount,
            "lender": story.lender,
            "canonical_url": citation.url,
            "source_name": citation.publisher or citation.source_id,
            "confidence": story.confidence,
            "published_at": story.published_at,
            "included_at": included_at,
            "recipient_group": recipient_group,
            "status": status,
            "notes": {"fact_ids": story.fact_ids, "source_ids": story.source_ids},
            "published_story_id": story.published_story_id,
            "candidate_id": story.candidate_id,
            "raw_event_id": story.raw_event_id,
            "run_id": edition.run_id,
            "edition_date": edition.edition_date,
            "selected_at": included_at if status is DigestRunStatus.READY_TO_SEND else "",
            "sent": status is DigestRunStatus.SENT,
            "source_ids": story.source_ids,
        }

    def upsert_published_story(
        self,
        story: Story,
        edition: Edition,
        *,
        send_id: str,
        recipient_group: str,
        status: DigestRunStatus,
        included_at: datetime,
    ) -> tuple[str, int]:
        return self._upsert_row(
            TAB_PUBLISHED_STORIES,
            self._published_story_row(
                story,
                edition,
                send_id=send_id,
                recipient_group=recipient_group,
                status=status,
                included_at=included_at,
            ),
            key_fields=("published_story_id",),
        )

    def upsert_published_stories(
        self,
        stories: Sequence[Story],
        edition: Edition,
        *,
        send_id: str,
        recipient_group: str,
        status: DigestRunStatus,
        included_at: datetime,
    ) -> list[tuple[str, int]]:
        rows = [
            self._published_story_row(
                story,
                edition,
                send_id=send_id,
                recipient_group=recipient_group,
                status=status,
                included_at=included_at,
            )
            for story in stories
        ]
        return self._upsert_rows(
            TAB_PUBLISHED_STORIES,
            rows,
            key_fields=("published_story_id",),
        )

    def upsert_digest_run(self, run: DigestRun) -> tuple[str, int]:
        row = run.model_dump(mode="python")
        row["section_counts_json"] = row.pop("section_counts")
        row["edition_json"] = _encode_persisted_edition(row["edition_json"])
        return self._upsert_row(
            TAB_DIGEST_RUNS,
            row,
            key_fields=("run_id", "status"),
        )


__all__ = [
    "GoogleSheetsRepository",
    "SheetRepositoryError",
    "SheetSchemaError",
    "SheetSecurityError",
    "serialize_sheet_value",
]
