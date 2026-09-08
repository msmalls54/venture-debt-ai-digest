"""Repair high-value inactive sources that failed their first fetch canary.

Every row must still exactly match the original staged definition and remain
inactive. The command only updates source configuration; it never collects,
calls a model, changes recipients, or sends email.
"""

from __future__ import annotations

import json

import gspread
from source_registry_expand_20260902 import HEADERS, ROWS, discovery

from vdai.config import AppSettings

AFTER: dict[str, list[str]] = {
    "S132": discovery(
        "S132",
        "Deposit Watch",
        "Bluevine",
        '"Bluevine" (banking OR lending OR credit OR funding OR partnership OR acquisition)',
        "US",
        "Small-business banking, deposits, lending, funding and partner-bank developments",
        priority="P1",
        lookback="180d",
    ),
    "S134": discovery(
        "S134",
        "Deposit Watch",
        "Lead Bank",
        '"Lead Bank" (fintech OR banking OR payments OR stablecoin OR partnership OR acquisition)',
        "US",
        "Fintech infrastructure, partner programs, payments and stablecoin developments",
        priority="P1",
        lookback="180d",
    ),
    "S160": discovery(
        "S160",
        "Deal Tape",
        "ORIX Growth Capital",
        '"ORIX Growth Capital" (investment OR debt OR credit OR financing)',
        "North America",
        "Growth lending, private-credit investments and portfolio financings",
        priority="P1",
        lookback="180d",
    ),
    "S177": discovery(
        "S177",
        "Deal Tape",
        "Clearco",
        "Clearco (credit OR financing OR funding OR acquisition OR partnership)",
        "Global",
        "Ecommerce growth capital, credit products and platform strategy",
        priority="P1",
        lookback="180d",
    ),
    "S178": discovery(
        "S178",
        "Deal Tape",
        "Pipe",
        '"Pipe" fintech (capital OR financing OR funding OR credit OR partnership)',
        "Global",
        "Embedded capital, financing products and platform partnerships",
        priority="P1",
        lookback="180d",
    ),
    "S179": discovery(
        "S179",
        "Deal Tape",
        "Wayflyer",
        "Wayflyer (financing OR funding OR credit facility OR debt OR partnership)",
        "Global",
        "Revenue-based financing, credit facilities and ecommerce lending",
        priority="P1",
        lookback="180d",
    ),
    "S182": discovery(
        "S182",
        "Runway Watch",
        "Canadian Business Growth Fund",
        '("Canadian Business Growth Fund" OR CBGF) (investment OR funding OR acquisition OR exit)',
        "Canada",
        "Growth-capital investments, exits and portfolio developments",
        priority="P1",
        lookback="180d",
    ),
    "S211": discovery(
        "S211",
        "Runway Watch",
        "FinSMEs",
        'site:finsmes.com (raises OR funding OR financing OR acquisition OR "credit facility")',
        "Global",
        "Startup funding, venture debt and acquisition discovery",
        priority="P1",
        lookback="7d",
    ),
    "S216": discovery(
        "S216",
        "Runway Watch",
        "Silicon Canals",
        "site:siliconcanals.com (raises OR funding OR financing OR acquisition OR fintech)",
        "Europe",
        "European startup funding, fintech and acquisition discovery",
        priority="P1",
        lookback="7d",
    ),
    "S217": discovery(
        "S217",
        "Competitive Field",
        "Private Credit Daily",
        '"Private Credit Daily" (financing OR direct lending OR fund OR acquisition)',
        "US",
        "Private-credit deals, funds, hires and lender strategy",
        priority="P1",
        lookback="30d",
    ),
}


def _modified(source_id: str, **changes: str) -> list[str]:
    row = list(ROWS[source_id])
    index_by_header = {header: index for index, header in enumerate(HEADERS)}
    for field, value in changes.items():
        row[index_by_header[field]] = value
    return row


AFTER.update(
    {
        "S185": _modified(
            "S185",
            url="https://docs.cohere.com/v2/changelog.md",
            method="Markdown",
            parser="Dated headings",
        ),
        "S186": _modified(
            "S186",
            url="https://docs.together.ai/docs/changelog.md",
            method="Markdown",
            parser="Dated headings",
        ),
        "S206": _modified(
            "S206",
            url="https://allenai.org/blog",
            method="HTML",
            parser="Article cards",
        ),
    }
)


def main() -> None:
    settings = AppSettings()
    client = gspread.service_account_from_dict(settings.service_account_info())
    worksheet = client.open_by_key(settings.google_sheet_id).worksheet("Sources")
    values = worksheet.get_all_values()
    if not values or tuple(values[0]) != HEADERS:
        raise RuntimeError("Sources headers changed; refusing to repair")

    row_by_id = {
        row[0]: (index, row)
        for index, row in enumerate(values[1:], start=2)
        if row and str(row[0]).strip()
    }
    mismatches: list[str] = []
    for source_id in AFTER:
        if source_id not in row_by_id:
            mismatches.append(source_id)
            continue
        actual = list(row_by_id[source_id][1])
        actual.extend([""] * (len(HEADERS) - len(actual)))
        if actual[: len(HEADERS)] != ROWS[source_id]:
            mismatches.append(source_id)
    if mismatches:
        raise RuntimeError(f"repair inputs changed unexpectedly: {sorted(mismatches)}")

    worksheet.batch_update(
        [
            {
                "range": f"A{row_by_id[source_id][0]}:V{row_by_id[source_id][0]}",
                "values": [AFTER[source_id]],
            }
            for source_id in sorted(AFTER)
        ],
        value_input_option="USER_ENTERED",
    )

    for source_id, expected in AFTER.items():
        row_index = row_by_id[source_id][0]
        actual = worksheet.get(f"A{row_index}:V{row_index}")[0]
        actual.extend([""] * (len(HEADERS) - len(actual)))
        if actual[: len(HEADERS)] != expected:
            raise RuntimeError(f"repair readback mismatch: {source_id}")

    print(
        json.dumps(
            {
                "status": "FAILED_SOURCES_REPAIRED_INACTIVE",
                "repaired_count": len(AFTER),
                "source_ids": sorted(AFTER),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
