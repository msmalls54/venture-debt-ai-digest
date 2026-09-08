"""Print a secret-safe snapshot of digest and legacy sender state."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import gspread

from vdai.agentmail import build_send_id
from vdai.config import AppSettings, normalize_key


def records(values: list[list[str]]) -> list[dict[str, str]]:
    if not values:
        return []
    headers = [normalize_key(value) for value in values[0]]
    return [
        {
            header: row[index] if index < len(row) else ""
            for index, header in enumerate(headers)
        }
        for row in values[1:]
        if any(str(value).strip() for value in row)
    ]


def main() -> None:
    settings = AppSettings()
    client = gspread.service_account_from_dict(settings.service_account_info())
    workbook = client.open_by_key(settings.google_sheet_id)
    digest_rows = records(workbook.worksheet("Digest Runs").get_all_values())
    health_rows = records(workbook.worksheet("Source Health").get_all_values())
    story_rows = records(workbook.worksheet("Published Stories").get_all_values())

    today_send_id = build_send_id(
        datetime.now(UTC).astimezone(ZoneInfo(settings.timezone)).date().isoformat()
    )
    sent_rows = [row for row in digest_rows if row.get("status", "").upper() == "SENT"]
    recent_runs = [
        {
            key: row.get(key, "")
            for key in (
                "run_id",
                "status",
                "send_id",
                "subject",
                "message_id",
                "thread_id",
                "accepted_at",
                "story_count",
            )
        }
        for row in digest_rows[-8:]
    ]
    legacy_health = [
        row
        for row in health_rows
        if not row.get("run_id", "").startswith(("RUN-", "TEST-"))
    ]
    cutoff = datetime.now(UTC) - timedelta(hours=48)
    recent_legacy = []
    for row in legacy_health:
        timestamp = row.get("updated_at") or row.get("checked_at") or row.get("last_attempt_at")
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if parsed.tzinfo is not None and parsed.astimezone(UTC) >= cutoff:
            recent_legacy.append(row)

    print(
        json.dumps(
            {
                "today_send_id": today_send_id,
                "today_sent_count": sum(
                    row.get("send_id", "") == today_send_id for row in sent_rows
                ),
                "sent_run_count": len(sent_rows),
                "published_story_count": len(story_rows),
                "recent_runs": recent_runs,
                "recent_published": [
                    {
                        key: row.get(key, "")
                        for key in (
                            "published_story_id",
                            "raw_event_id",
                            "headline",
                            "source_ids_json",
                            "published_at",
                            "send_id",
                        )
                    }
                    for row in story_rows[-15:]
                ],
                "legacy_health_row_count": len(legacy_health),
                "legacy_health_rows_last_48h": len(recent_legacy),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
