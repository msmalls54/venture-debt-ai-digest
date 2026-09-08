"""Activate only source IDs that passed the 2026-09-02 quality canary.

This command changes the Sources checkbox only. It does not collect content,
call a model, alter recipients, or send email.
"""

from __future__ import annotations

import argparse
import json

import gspread

from vdai.config import AppSettings

VETTED_EXISTING = frozenset({"S040", "S053", "S063", "S094"})
VETTED_EXPANSION = frozenset(
    {
        "S131",
        "S135",
        "S136",
        "S137",
        "S138",
        "S139",
        "S140",
        "S141",
        "S144",
        "S145",
        "S147",
        "S149",
        "S150",
        "S155",
        "S156",
        "S157",
        "S158",
        "S161",
        "S170",
        "S171",
        "S172",
        "S174",
        "S175",
        "S188",
        "S189",
        "S190",
        "S191",
        "S192",
        "S199",
        "S200",
        "S201",
        "S202",
        "S203",
        "S205",
        "S210",
        "S212",
        "S214",
        "S215",
        "S218",
        "S220",
        "S221",
        "S222",
        "S224",
    }
)
VETTED_REPAIRS = frozenset({"S132", "S177", "S178", "S179", "S209", "S211"})
VETTED = VETTED_EXISTING | VETTED_EXPANSION | VETTED_REPAIRS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-id", action="append", dest="source_ids")
    args = parser.parse_args()
    requested = set(args.source_ids or VETTED)
    unsupported = requested - VETTED
    if unsupported:
        raise RuntimeError(f"source IDs were not quality-vetted: {sorted(unsupported)}")

    settings = AppSettings()
    client = gspread.service_account_from_dict(settings.service_account_info())
    worksheet = client.open_by_key(settings.google_sheet_id).worksheet("Sources")
    values = worksheet.get_all_values()
    if not values or len(values[0]) < 11 or values[0][0] != "source_id":
        raise RuntimeError("Sources schema changed; refusing to activate")
    if values[0][10] != "active":
        raise RuntimeError("Sources active column moved; refusing to activate")

    row_by_id = {
        str(row[0]).strip(): index
        for index, row in enumerate(values[1:], start=2)
        if row and str(row[0]).strip()
    }
    missing = requested - set(row_by_id)
    if missing:
        raise RuntimeError(f"vetted source IDs are missing: {sorted(missing)}")

    worksheet.batch_update(
        [
            {"range": f"K{row_by_id[source_id]}", "values": [["TRUE"]]}
            for source_id in sorted(requested)
        ],
        value_input_option="USER_ENTERED",
    )

    active_after = {
        source_id
        for source_id in requested
        if str(worksheet.acell(f"K{row_by_id[source_id]}").value).strip().upper() == "TRUE"
    }
    if active_after != requested:
        raise RuntimeError("activation readback was incomplete")

    print(
        json.dumps(
            {
                "status": "VETTED_SOURCES_ACTIVATED",
                "activated_count": len(active_after),
                "existing_recovered": len(active_after & VETTED_EXISTING),
                "newly_expanded": len(active_after & VETTED_EXPANSION),
                "repair_recoveries": len(active_after & VETTED_REPAIRS),
                "source_ids": sorted(active_after),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
