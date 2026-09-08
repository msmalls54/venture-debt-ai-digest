"""Idempotently add validated recipients to the digest's Google Sheet."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence

import gspread
from pydantic import ValidationError

from vdai.config import AppSettings, normalize_key
from vdai.models import Recipient


def _normalized_emails(values: Sequence[str]) -> list[str]:
    emails: list[str] = []
    seen: set[str] = set()
    for value in values:
        try:
            email = str(Recipient(email=value.strip().replace("\\@", "@")).email)
        except ValidationError as error:
            raise ValueError(f"invalid recipient email: {value!r}") from error
        if email not in seen:
            seen.add(email)
            emails.append(email)
    return emails


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("emails", nargs="+")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    requested = _normalized_emails(args.emails)
    settings = AppSettings()
    client = gspread.service_account_from_dict(settings.service_account_info())
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    worksheet = spreadsheet.worksheet("Recipients")
    # Recipient data lives only in A:C. Other formatted/default columns can contain
    # values through row 1000 and must not influence the append boundary.
    values = worksheet.get("A:C")
    if not values:
        raise RuntimeError("Recipients is missing its header row")
    headers = [normalize_key(value) for value in values[0]]
    expected = ["recipient_id", "email", "display_name"]
    if headers[:3] != expected:
        raise RuntimeError("Recipients headers changed; refusing to write")

    populated_row_numbers = [
        index
        for index, row in enumerate(values, start=1)
        if any(str(cell).strip() for cell in row)
    ]
    last_populated_row = max(populated_row_numbers, default=1)
    existing_emails = {
        row[1].strip().lower()
        for row in values[1:]
        if len(row) > 1 and row[1].strip()
    }
    missing = [email for email in requested if email not in existing_emails]
    numeric_ids = [
        int(match.group(1))
        for row in values[1:]
        if row and (match := re.fullmatch(r"R(\d+)", row[0].strip(), re.IGNORECASE))
    ]
    next_id = max(numeric_ids, default=0) + 1
    rows = [
        [f"R{next_id + index:03d}", email, ""]
        for index, email in enumerate(missing)
    ]

    if rows and args.apply:
        # Grid formatting can make get_all_values return hundreds of blank rows.
        # Append after the last row with real content, not after the formatted grid.
        start_row_index = last_populated_row
        end_row_index = start_row_index + len(rows)
        spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "copyPaste": {
                            "source": {
                                "sheetId": worksheet.id,
                                "startRowIndex": 1,
                                "endRowIndex": 2,
                                "startColumnIndex": 0,
                                "endColumnIndex": 3,
                            },
                            "destination": {
                                "sheetId": worksheet.id,
                                "startRowIndex": start_row_index,
                                "endRowIndex": end_row_index,
                                "startColumnIndex": 0,
                                "endColumnIndex": 3,
                            },
                            "pasteType": "PASTE_FORMAT",
                            "pasteOrientation": "NORMAL",
                        }
                    },
                    {
                        "updateCells": {
                            "range": {
                                "sheetId": worksheet.id,
                                "startRowIndex": start_row_index,
                                "endRowIndex": end_row_index,
                                "startColumnIndex": 0,
                                "endColumnIndex": 3,
                            },
                            "rows": [
                                {
                                    "values": [
                                        {"userEnteredValue": {"stringValue": value}}
                                        for value in row
                                    ]
                                }
                                for row in rows
                            ],
                            "fields": "userEnteredValue",
                        }
                    },
                ]
            }
        )

    print(
        json.dumps(
            {
                "status": "APPLIED" if args.apply else "PREVIEW",
                "spreadsheet_id": spreadsheet.id,
                "sheet": worksheet.title,
                "existing_count": len(existing_emails),
                "requested_count": len(requested),
                "added_count": len(rows) if args.apply else 0,
                "already_present": [email for email in requested if email in existing_emails],
                "rows": rows,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
