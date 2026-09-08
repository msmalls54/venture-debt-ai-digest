"""Apply the audited 2026-08-30 source-registry repair without touching sends."""

from __future__ import annotations

import argparse
import json

import gspread

from vdai.config import AppSettings

HEADERS = (
    "source_id",
    "section",
    "organization",
    "source_name",
    "url",
    "method",
    "parser",
    "cadence",
    "source_tier",
    "priority",
    "active",
    "region",
    "notes",
    "allowed_domains",
    "primary_source",
    "discovery_only",
    "headers_profile",
    "max_items",
    "max_pages",
    "max_bytes",
    "parser_version",
)


def row(*values: str) -> list[str]:
    if len(values) != len(HEADERS):
        raise ValueError(f"source row has {len(values)} values; expected {len(HEADERS)}")
    return list(values)


STAGED_ROWS = {
    "S066": row(
        "S066", "AI Radar", "xAI", "Developer release notes",
        "https://docs.x.ai/developers/release-notes", "HTML", "HTML page",
        "Every 4 hours", "3-primary", "P0", "FALSE", "Global",
        "Official release-note page; content changes are tracked as one dated document",
        "docs.x.ai", "TRUE", "FALSE", "STANDARD", "10", "1", "2000000",
        "candidate.v1",
    ),
    "S076": row(
        "S076", "AI Radar", "Cerebras", "Official blog",
        "https://www.cerebras.ai/blog", "HTML", "Article cards", "Every 4 hours",
        "3-primary", "P0", "FALSE", "Global",
        "Models, inference economics, hardware and major partnerships", "cerebras.ai",
        "TRUE", "FALSE", "STANDARD", "20", "1", "2000000", "candidate.v1",
    ),
    "S077": row(
        "S077", "AI Radar", "Cohere", "Official newsroom",
        "https://cohere.com/newsroom", "HTML", "HTML detail", "Every 4 hours",
        "3-primary", "P1", "FALSE", "Global",
        "Model, product and corporate announcements; hydrate bounded detail pages for dates",
        "cohere.com", "TRUE", "FALSE", "STANDARD", "20", "5", "2000000",
        "candidate.v1",
    ),
    "S078": row(
        "S078", "AI Radar", "Artificial Analysis", "Benchmark articles",
        "https://artificialanalysis.ai/articles", "HTML", "Article cards", "Daily",
        "2-discovery", "P0", "FALSE", "Global",
        "Independent model, agent, cost, speed and provider benchmarks",
        "artificialanalysis.ai", "FALSE", "TRUE", "STANDARD", "20", "1",
        "2000000", "candidate.v1",
    ),
    "S079": row(
        "S079", "AI Radar", "LMArena", "Official benchmark blog",
        "https://arena.ai/blog", "HTML", "HTML detail", "Daily", "B", "P1",
        "FALSE", "Global",
        "Human-preference leaderboard and evaluation methodology changes; hydrate detail dates",
        "arena.ai;blog.lmarena.ai", "FALSE", "FALSE", "STANDARD", "20", "6",
        "2000000", "candidate.v1",
    ),
    "S081": row(
        "S081", "AI Radar", "Baidu ERNIE", "Official model blog",
        "https://ernie.baidu.com/blog/", "HTML", "Article cards", "Every 4 hours",
        "3-primary", "P0", "FALSE", "China",
        "ERNIE model releases and official benchmark disclosures", "ernie.baidu.com",
        "TRUE", "FALSE", "STANDARD", "20", "1", "2000000", "candidate.v1",
    ),
    "S082": row(
        "S082", "AI Radar", "Tencent Hunyuan", "Official GitHub releases",
        "https://api.github.com/orgs/Tencent-Hunyuan/repos?sort=created&direction=desc&per_page=20",
        "API", "JSON", "Every 4 hours", "3-primary", "P0", "FALSE", "China",
        "Official Hunyuan repositories ordered by creation date; model releases only",
        "api.github.com;github.com", "TRUE", "FALSE", "STANDARD", "20", "1",
        "2000000", "candidate.v1",
    ),
    "S084": row(
        "S084", "AI Radar", "Alibaba Qwen", "Official model articles API",
        "https://qwen.ai/api/v2/article/retrieval?type=qwen_ai&language=en-US",
        "API", "JSON", "Every 4 hours", "3-primary", "P0", "FALSE", "China",
        "Qwen model, agent and multimodal releases from the official article service",
        "qwen.ai", "TRUE", "FALSE", "STANDARD", "20", "1", "5000000",
        "candidate.v1",
    ),
    "S086": row(
        "S086", "AI Radar", "ByteDance Seed", "Official model blog",
        "https://seed.bytedance.com/blog", "HTML", "Article cards", "Every 4 hours",
        "3-primary", "P0", "FALSE", "China", "Seed and Doubao model research and releases",
        "seed.bytedance.com", "TRUE", "FALSE", "STANDARD", "20", "1", "2000000",
        "candidate.v1",
    ),
    "S091": row(
        "S091", "Deal Tape", "Blue Owl Capital", "Official news",
        "https://www.blueowl.com/news", "HTML", "Article cards", "Daily", "3-primary",
        "P0", "FALSE", "Global",
        "Technology finance, private credit and AI infrastructure financings",
        "blueowl.com", "TRUE", "FALSE", "COMPAT", "20", "1", "2000000",
        "candidate.v1",
    ),
    "S097": row(
        "S097", "Deal Tape", "TriplePoint Venture Growth", "Official SEC submissions",
        "https://data.sec.gov/submissions/CIK0001580345.json", "API", "JSON", "Daily",
        "3-regulatory", "P0", "FALSE", "US",
        "Exact TPVG filing stream; retained as a proven backup because S003 already "
        "covers this CIK",
        "data.sec.gov;www.sec.gov", "TRUE", "FALSE", "SEC", "30", "1", "2000000",
        "candidate.v1",
    ),
    "S100": row(
        "S100", "Deal Tape", "Partners for Growth", "Official firm RSS",
        "https://www.pfgrowth.com/feed/", "RSS", "RSS/Atom", "Daily", "3-primary",
        "P1", "FALSE", "Global",
        "Growth debt, private credit and portfolio operating updates; filter generic promotion",
        "pfgrowth.com", "TRUE", "FALSE", "STANDARD", "20", "1", "1000000",
        "candidate.v1",
    ),
    "S101": row(
        "S101", "Deal Tape", "Viola Credit", "Official venture-credit insights RSS",
        "https://violacredit.com/feed/", "RSS", "RSS/Atom", "Daily", "3-primary", "P0",
        "FALSE", "Global",
        "Venture debt, growth lending and fintech-credit analysis from Viola Credit",
        "violacredit.com", "TRUE", "FALSE", "STANDARD", "20", "1", "1000000",
        "candidate.v1",
    ),
    "S102": row(
        "S102", "Deal Tape", "Avidbank", "Official SEC submissions",
        "https://data.sec.gov/submissions/CIK0001443575.json", "API", "JSON", "Daily",
        "3-regulatory", "P0", "FALSE", "US",
        "Exact Avidbank filing stream used because its official newsroom blocks automation",
        "data.sec.gov;www.sec.gov", "TRUE", "FALSE", "SEC", "30", "1", "2000000",
        "candidate.v1",
    ),
    "S103": row(
        "S103", "Deal Tape", "Espresso Capital", "Official newsroom",
        "https://espressocapital.com/resources/newsroom/", "HTML", "Article cards", "Daily",
        "3-primary", "P0", "FALSE", "Global",
        "Venture-debt facilities, borrower announcements and firm news",
        "espressocapital.com", "TRUE", "FALSE", "COMPAT", "30", "1", "2000000",
        "candidate.v1",
    ),
    "S104": row(
        "S104", "Deal Tape", "Comerica Technology and Life Sciences",
        "Legacy specialty-lending reference",
        "https://www.comerica.com/business/solutions/specialized-industries/technology.html",
        "HTML", "HTML page", "Weekly", "1-discovery", "P2", "FALSE", "US",
        "Reference only: Comerica was acquired by Fifth Third; existing S024 is retained "
        "for lineage, not used as an active news feed",
        "comerica.com", "FALSE", "TRUE", "STANDARD", "10", "1", "1000000",
        "candidate.v1",
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--activate", nargs="*", default=[])
    args = parser.parse_args()

    settings = AppSettings()
    client = gspread.service_account_from_dict(settings.service_account_info())
    spreadsheet = client.open_by_key(settings.google_sheet_id)
    worksheet = spreadsheet.worksheet("Sources")
    values = worksheet.get_all_values()
    if tuple(values[0]) != HEADERS:
        raise RuntimeError("Sources headers changed; refusing to write")

    positions = {current[0]: index for index, current in enumerate(values[1:], start=2) if current}
    new_ids = [source_id for source_id in STAGED_ROWS if source_id not in positions]
    expected_new = {"S101", "S102", "S103", "S104"}
    if set(new_ids) - expected_new:
        raise RuntimeError(f"unexpected missing source rows: {new_ids}")

    # The registry has preformatted blank rows. Copy the established source-row
    # format and validation rules before inserting new values.
    if new_ids:
        destination_rows = [int(source_id[1:]) + 1 for source_id in new_ids]
        start_row = min(destination_rows) - 1
        end_row = max(destination_rows)
        requests = []
        for paste_type in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION"):
            requests.append(
                {
                    "copyPaste": {
                        "source": {
                            "sheetId": worksheet.id,
                            "startRowIndex": 100,
                            "endRowIndex": 101,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(HEADERS),
                        },
                        "destination": {
                            "sheetId": worksheet.id,
                            "startRowIndex": start_row,
                            "endRowIndex": end_row,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(HEADERS),
                        },
                        "pasteType": paste_type,
                    }
                }
            )
        spreadsheet.batch_update({"requests": requests})

    activation = set(args.activate)
    unknown_activation = activation - set(STAGED_ROWS)
    if unknown_activation:
        raise RuntimeError(f"unknown activation IDs: {sorted(unknown_activation)}")
    for source_id, source_values in STAGED_ROWS.items():
        output = list(source_values)
        if source_id in activation:
            output[10] = "TRUE"
        row_number = positions.get(source_id, int(source_id[1:]) + 1)
        worksheet.update(
            range_name=f"A{row_number}:U{row_number}",
            values=[output],
            value_input_option="USER_ENTERED",
        )

    readback = worksheet.get("A66:U105")
    by_id = {current[0]: current for current in readback if current and current[0] in STAGED_ROWS}
    if set(by_id) != set(STAGED_ROWS):
        raise RuntimeError("source write readback was incomplete")
    for source_id, expected in STAGED_ROWS.items():
        actual = by_id[source_id]
        expected_active = "TRUE" if source_id in activation else "FALSE"
        if actual[:10] != expected[:10] or actual[10].upper() != expected_active:
            raise RuntimeError(f"source readback mismatch: {source_id}")
    print(
        json.dumps(
            {
                "status": "SOURCE_REGISTRY_UPDATED",
                "updated": sorted(STAGED_ROWS),
                "activated": sorted(activation),
                "kept_inactive": sorted(set(STAGED_ROWS) - activation),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
