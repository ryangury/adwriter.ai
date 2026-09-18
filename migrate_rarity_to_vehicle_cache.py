#!/usr/bin/env python3
"""migrate_rarity_to_vehicle_cache.py — one-time backfill: warm vehicle_cache.db
from rarity.db's existing 'complete' window-sticker corpus, at zero scrape cost.

rarity.db (vehicle_equipment) stores window stickers keyed by VIN across every
dealership vin_importer.py or the iPacket browse crawl has ever touched, going
back further than vehicle_cache.db has existed. Every 'complete' row there is a
window sticker vehicle_cache.db doesn't need to re-scrape.

rarity.db's schema keeps `option_packages` (priced lines only) and
`standard_options`, but never stored `added_options_all` — the interleaved list
a package's unpriced sub_items are derived from (see
aggregator._packages_with_sub_items()). Re-parsing the stored `raw_sticker_text`
through AutoiPacketScraper._parse_sticker_text() (a pure-text static method —
no browser involved) reconstructs `added_options_all` from scratch, so a
migrated row carries the same package-contents detail a fresh scrape would.
Rows with no raw_sticker_text (a handful of older imports) fall back to the
stored columns directly, with empty sub_items on every package — the same
limitation the live rarity_db_cache tier already has for those VINs.

A VIN already cached in vehicle_cache.db is left alone (never overwritten) —
this only fills gaps.

    python3 migrate_rarity_to_vehicle_cache.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from scraper import AutoiPacketScraper
from vehicle_cache import needs_window_sticker, save_window_sticker

RARITY_DB_PATH = Path(__file__).with_name("rarity.db")


def _year_make_model(row: sqlite3.Row) -> str | None:
    parts = [str(row[k]) for k in ("year", "make", "model", "trim") if row[k]]
    return " ".join(parts) or None


def _sticker_data_for_row(row: sqlite3.Row) -> dict[str, Any]:
    """Reconstruct a pull_sticker()-shaped dict for one rarity.db row.
    Prefers re-parsing raw_sticker_text (recovers added_options_all / real
    sub_items); falls back to the stored columns (no sub_items) when there's
    no raw text to re-parse."""
    raw_text = row["raw_sticker_text"]
    if raw_text:
        data = AutoiPacketScraper._parse_sticker_text(raw_text, row["vin"])
        # the stored interior_color column exists for rows imported after that
        # column was added — prefer it when the re-parse didn't find one.
        if not data.get("interior_color") and row["interior_color"]:
            data["interior_color"] = row["interior_color"]
        return data

    return {
        "vin": row["vin"],
        "year_make_model": _year_make_model(row),
        "exterior_color": row["exterior_color"],
        "interior_color": row["interior_color"],
        "base_price": row["base_price"],
        "freight": row["freight"],
        "total_msrp": row["total_msrp"],
        "option_packages": json.loads(row["option_packages"] or "[]"),
        "added_options_all": [],  # not recoverable without raw_sticker_text
        "standard_options": json.loads(row["standard_options"] or "[]"),
        "raw_text": "",
        "sticker_url": None,
        "render": "pdf" if row["source"] == "autoipacket_pdf" else "html",
    }


def migrate(*, dry_run: bool = False) -> int:
    if not RARITY_DB_PATH.exists():
        print(f"No rarity.db at {RARITY_DB_PATH} — nothing to migrate.")
        return 1

    conn = sqlite3.connect(RARITY_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM vehicle_equipment WHERE status = 'complete'"
    ).fetchall()
    conn.close()

    print(f"rarity.db: {len(rows)} 'complete' row(s) found")

    counts = {"migrated": 0, "already_cached": 0, "unusable": 0}
    for row in rows:
        vin = (row["vin"] or "").strip().upper()
        if not vin:
            counts["unusable"] += 1
            continue

        if not needs_window_sticker(vin):
            counts["already_cached"] += 1
            continue

        sticker_data = _sticker_data_for_row(row)
        if sticker_data.get("total_msrp") is None and not sticker_data.get(
            "option_packages"
        ):
            counts["unusable"] += 1
            continue

        source = row["source"] or "rarity_db_migration"
        if dry_run:
            print(f"[dry-run] would cache {vin} ({_year_make_model(row)}, source={source})")
        else:
            save_window_sticker(
                vin, None, sticker_data.get("year_make_model") or _year_make_model(row),
                sticker_data, source,
            )
            print(f"cached {vin} ({_year_make_model(row)}, source={source})")
        counts["migrated"] += 1

    print()
    print("=" * 50)
    print("RARITY -> VEHICLE_CACHE MIGRATION SUMMARY" + (" (dry run)" if dry_run else ""))
    print("=" * 50)
    print(f"  Migrated:              {counts['migrated']}")
    print(f"  Already in vehicle_cache.db (skipped): {counts['already_cached']}")
    print(f"  Unusable (skipped):    {counts['unusable']}")
    print(f"  Total rarity.db rows:  {len(rows)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill vehicle_cache.db from rarity.db's complete sticker corpus"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be migrated, write nothing"
    )
    args = parser.parse_args(argv)
    return migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
