#!/usr/bin/env python3
"""carfax_warmup.py — bulk-warm vehicle_cache.db's Carfax data for the whole
retail inventory, so ad writes and reprices can skip the Carfax scrape for any
vehicle whose report is already cached and fresh.

Reads last_inventory_snapshot.json — every vehicle the inventory crawler
mapped as retail, on any status code (the crawler already filters to
objective == "Retail" before saving the snapshot, so no status_code filter is
applied here). For each VIN whose Carfax is not yet cached, or is cached but
older than vehicle_cache.CARFAX_TTL days (vehicle_cache.needs_carfax() is
True), opens the ACV Max pricing page directly by vehicle_id (skipping the
inventory search — a fresh search per vehicle isn't guaranteed to resolve to
the same row twice) and scrapes the Carfax report in a single browser session.

A single bad vehicle (pricing iframe never loads, Carfax report never opens,
a wrong-vehicle mismatch, any other scraper error) is logged and skipped — it
never stops the run. A scrape that comes back empty (no owners, no odometer
reading — the same signal the scraper itself uses to flag a bad parse) is
treated as a failure and is never cached.

    python3 carfax_warmup.py [--limit N] [--headed]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from aggregator import _apply_carfax_vision
from scraper import ACVMaxScraper, ScraperError, VehicleIdentityError
from vehicle_cache import needs_carfax, save_carfax

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


def _owner_summary(carfax_data: dict[str, Any]) -> str:
    owners = carfax_data.get("number_of_owners")
    owner_str = "owners unknown" if owners is None else f"{owners} owner{'' if owners == 1 else 's'}"
    accident_str = "no accidents" if carfax_data.get("no_accidents") else "accident(s) reported"
    return f"{owner_str}, {accident_str}"


def _is_empty_scrape(carfax_data: dict[str, Any]) -> bool:
    """Mirrors the scraper's own empty-parse signal (scrape_carfax dumps debug
    output under this same condition) — neither field ever coming back means
    the report page didn't render, so there's nothing worth caching."""
    return not carfax_data.get("number_of_owners") and not carfax_data.get("last_reported_odometer")


def _check_identity(acv: ACVMaxScraper, frame, stock: str, vin: str) -> None:
    """Guard against a stale/wrong pricing page — vehicle_id is passed straight
    through without a fresh search, so nothing else confirms we're looking at
    the vehicle we think we are."""
    text = frame.locator("body").inner_text()
    parsed = ACVMaxScraper._parse_pricing_text(text, stock)
    resolved_vin = (parsed.get("vin") or "").strip().upper()
    resolved_stock = (parsed.get("stock_number") or "").strip().upper()
    expected_vin = vin.strip().upper()
    expected_stock = stock.strip().lstrip("#").upper()
    if resolved_stock and resolved_stock != expected_stock:
        raise VehicleIdentityError(
            f"Requested stock #{expected_stock} but the pricing page resolved "
            f"to stock #{resolved_stock} — wrong vehicle, refusing to scrape Carfax."
        )
    if resolved_vin and resolved_vin != expected_vin:
        raise VehicleIdentityError(
            f"Stock #{expected_stock}: pricing page VIN {resolved_vin!r} does "
            f"not match expected VIN {expected_vin!r} — wrong vehicle, refusing "
            f"to scrape Carfax."
        )


def warmup(*, limit: int | None = None, headless: bool = True) -> int:
    vehicles = _load_retail_vehicles()
    if limit:
        vehicles = vehicles[:limit]
    total = len(vehicles)

    counts = {
        "checked": 0,
        "skipped_cached": 0,
        "skipped": 0,
        "newly_cached": 0,
        "vision_parsed": 0,
        "failed": 0,
        "failed_iframe_timeout": 0,
    }

    with ACVMaxScraper(headless=headless, use_saved_session=True) as acv:
        acv.login()

        for i, v in enumerate(vehicles, 1):
            counts["checked"] += 1
            stock = v.get("stock_number")
            vin = v.get("vin")
            vehicle_id = v.get("vehicle_id")

            if stock and stock.upper().startswith("Z"):
                counts["skipped"] += 1
                print(
                    f"[carfax_warmup] skipping Z stock {stock} — no Carfax "
                    f"history expected"
                )
                continue

            if not stock or not vin or not vehicle_id:
                counts["failed"] += 1
                print(
                    f"[carfax_warmup] {i} of {total} — {stock or vin or '?'} — "
                    f"failed (missing stock number, VIN, or vehicle_id)",
                    file=sys.stderr,
                )
                continue

            if not needs_carfax(vin):
                counts["skipped_cached"] += 1
                print(
                    f"[carfax_warmup] {i} of {total} — {stock} — "
                    f"skipped (already cached, fresh)"
                )
                continue

            try:
                frame, _url = acv.open_pricing(vehicle_id)
                _check_identity(acv, frame, stock, vin)
                carfax_data = acv.scrape_carfax(acvmax_mileage=v.get("mileage"))
            except ScraperError as exc:
                if "Merchandising iframe" in str(exc) and "never appeared" in str(exc):
                    counts["failed_iframe_timeout"] += 1
                    print(
                        f"[carfax_warmup] {stock} — failed (iframe timeout) — "
                        f"will retry next run",
                        file=sys.stderr,
                    )
                else:
                    counts["failed"] += 1
                    print(
                        f"[carfax_warmup] {i} of {total} — {stock} — failed: {exc}",
                        file=sys.stderr,
                    )
                continue
            except Exception as exc:  # noqa: BLE001 - one vehicle must never kill the run
                counts["failed"] += 1
                print(
                    f"[carfax_warmup] {i} of {total} — {stock} — failed: {exc}",
                    file=sys.stderr,
                )
                continue

            if _is_empty_scrape(carfax_data):
                counts["failed"] += 1
                print(
                    f"[carfax_warmup] {i} of {total} — {stock} — "
                    f"failed (Carfax report loaded but returned no data)",
                    file=sys.stderr,
                )
                continue

            # Vision-parse the screenshot scrape_carfax() just captured (if
            # any) before caching — same "vision primary, text-parse fallback"
            # merge the live ad-write pipeline uses (see
            # aggregator._apply_carfax_vision()'s docstring for what it does
            # and doesn't overwrite).
            carfax_data = _apply_carfax_vision(carfax_data, vin)
            if carfax_data.get("carfax_parse_source") == "vision":
                counts["vision_parsed"] += 1

            save_carfax(
                vin, stock, carfax_data,
                image_path=carfax_data.get("carfax_image_path"),
            )
            counts["newly_cached"] += 1
            print(
                f"[carfax_warmup] {i} of {total} — {stock} — "
                f"cached ({_owner_summary(carfax_data)})"
                + (
                    " [vision]"
                    if carfax_data.get("carfax_parse_source") == "vision"
                    else ""
                )
            )

    print()
    print("Carfax warmup complete")
    print(f"Vehicles checked: {counts['checked']}")
    print(f"Already cached (skipped): {counts['skipped_cached']}")
    print(f"Skipped (Z stock, no Carfax history): {counts['skipped']}")
    print(f"Newly cached: {counts['newly_cached']}")
    print(f"  of which vision-parsed: {counts['vision_parsed']}")
    print(f"Failed: {counts['failed']}")
    print(f"Failed (iframe timeout, will retry): {counts['failed_iframe_timeout']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Warm vehicle_cache.db's Carfax data for the whole retail inventory"
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
