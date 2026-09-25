#!/usr/bin/env python3
"""recon_warmup.py — bulk-warm vehicle_cache.db's recon data for the whole
retail inventory, so ad writes and reprices can skip ReconVision for any
vehicle whose recon is already cached and complete.

Reads last_inventory_snapshot.json — every vehicle the inventory crawler
mapped as retail, on any status code (10/11/12/16, and anything else present,
e.g. 1 = needs certification assigned). For each VIN whose recon is not yet
cached, or is cached but not yet complete (vehicle_cache.needs_recon() is
True), pulls the work order from ReconVision by stock number in a single
browser session and caches the result. recon_complete is decided by
aggregator._recon_is_complete() — the same "Close RO status, else every
service item done" rule the ad-writer pipeline uses, so this script and
aggregate() never disagree about whether a vehicle's recon is done.

A single bad vehicle (no work order found, a ReconVision timeout, any other
scraper error) is logged and skipped — it never stops the run.

    python3 recon_warmup.py [--limit N] [--headed]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from aggregator import _recon_is_complete
from scraper import ReconVisionScraper, ScraperError
from vehicle_cache import needs_recon, save_recon

SNAPSHOT_PATH = Path(__file__).with_name("last_inventory_snapshot.json")


def _load_retail_vehicles() -> list[dict[str, Any]]:
    if not SNAPSHOT_PATH.exists():
        print(
            f"No {SNAPSHOT_PATH.name} found — run the inventory crawler first.",
            file=sys.stderr,
        )
        return []
    try:
        data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Could not read {SNAPSHOT_PATH.name}: {exc}", file=sys.stderr)
        return []
    return data.get("vehicles") or []


def warmup(*, limit: int | None = None, headless: bool = True) -> int:
    vehicles = _load_retail_vehicles()
    if limit:
        vehicles = vehicles[:limit]
    total = len(vehicles)

    counts = {
        "checked": 0,
        "skipped_cached": 0,
        "complete": 0,
        "pending": 0,
        "failed": 0,
    }

    with ReconVisionScraper(headless=headless, use_saved_session=True) as rv:
        rv.login()

        for i, v in enumerate(vehicles, 1):
            counts["checked"] += 1
            stock = v.get("stock_number")
            vin = v.get("vin")

            if not stock or not vin:
                counts["failed"] += 1
                print(
                    f"[recon_warmup] {i} of {total} — {stock or vin or '?'} — "
                    f"failed (missing stock number or VIN)",
                    file=sys.stderr,
                )
                continue

            if not needs_recon(vin):
                counts["skipped_cached"] += 1
                print(
                    f"[recon_warmup] {i} of {total} — {stock} — "
                    f"skipped (already cached, recon complete)"
                )
                continue

            try:
                recon_data = rv.scrape_work_order(stock)
            except ScraperError as exc:
                counts["failed"] += 1
                print(
                    f"[recon_warmup] {i} of {total} — {stock} — failed: {exc}",
                    file=sys.stderr,
                )
                continue
            except Exception as exc:  # noqa: BLE001 - one vehicle must never kill the run
                counts["failed"] += 1
                print(
                    f"[recon_warmup] {i} of {total} — {stock} — failed: {exc}",
                    file=sys.stderr,
                )
                continue

            # recon_image_bytes is in-process only (see ReconVisionScraper.
            # scrape_work_order()'s docstring note) — never json.dumps()'d.
            recon_data.pop("recon_image_bytes", None)

            # DOM-scraped line items are cached as-is — no vision overlay (see
            # aggregator._apply_recon_vision()'s docstring).
            recon_complete = _recon_is_complete(recon_data.get("line_items", []))
            save_recon(
                vin, stock, recon_data, recon_complete,
                image_path=recon_data.get("recon_image_path"),
            )
            counts["complete" if recon_complete else "pending"] += 1
            print(
                f"[recon_warmup] {i} of {total} — {stock} — complete "
                f"(recon_complete: {recon_complete})"
            )

    print()
    print("=" * 50)
    print("Recon warmup complete")
    print(f"  Vehicles checked:                        {counts['checked']}")
    print(f"  Already cached — skipped:                {counts['skipped_cached']}")
    print(f"  Newly cached — recon complete:            {counts['complete']}")
    print(f"  Newly cached — recon pending:              {counts['pending']}")
    print(f"  Failed (no work order or scraper error): {counts['failed']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Warm vehicle_cache.db's recon data for the whole retail inventory"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="process only N vehicles (for testing)"
    )
    parser.add_argument(
        "--headed", action="store_true", help="show the browser window"
    )
    args = parser.parse_args(argv)
    return warmup(limit=args.limit, headless=not args.headed)


if __name__ == "__main__":
    raise SystemExit(main())
