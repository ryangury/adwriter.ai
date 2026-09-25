#!/usr/bin/env python3
"""ctr_warmup.py — capture CTR (click-through-rate) snapshots into
ctr_history.db: Durham's own retail inventory plus the Northlake/Charlotte
competitive benchmark stores.

This is the same "5. DURHAM CTR CAPTURE" / "6. BENCHMARK CTR CAPTURE" work
orchestrator.py's full daily run does — extracted here (orchestrator.py now
imports and calls capture_durham_ctr() / capture_benchmark_ctr() from this
module, so there's exactly one implementation) so it can run on its own daily
schedule instead of only riding along with the rest of the pipeline, which
was previously the only trigger for CTR capture and only fired Friday/
Saturday at 5am plus whatever ad-hoc orchestrator runs happened to occur —
most days got zero new CTR rows as a result.

Reads last_inventory_snapshot.json for the Durham vehicle list (same source
recon_warmup.py / carfax_warmup.py use) rather than doing its own fresh
crawl, so a normal daily run doesn't pay for a second full inventory crawl on
top of whatever already ran that morning.

A single bad vehicle (pricing iframe never loads, CTR graph never renders,
any other scraper error) is logged and skipped — it never stops the run.

    python3 ctr_warmup.py             # full run, both steps
    python3 ctr_warmup.py --dry-run   # scrape and log, but skip the DB write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ctr_database import BENCHMARK_DEALERSHIPS, infer_tier, record_ctr
from scraper import ACVMaxScraper, ScraperError

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


def capture_durham_ctr(
    acv: ACVMaxScraper,
    retail: list[dict[str, Any]],
    *,
    ad_history: dict[str, Any] | None = None,
    aggregated_ctr: dict[str, Any] | None = None,
    dry_run: bool = False,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Durham retail CTR capture for every vehicle in `retail`. `acv` must
    already be logged in. aggregated_ctr lets a caller that already scraped a
    vehicle's CTR this run (orchestrator.py, during ad generation) skip a
    redundant scrape for it; ctr_warmup.py's own standalone run passes none,
    so every vehicle gets a fresh scrape."""
    ad_history = ad_history or {}
    aggregated_ctr = aggregated_ctr or {}
    if errors is None:
        errors = []
    counts = {"attempted": 0, "recorded": 0, "failed": 0}
    total = len(retail)
    for i, v in enumerate(retail, 1):
        stock = v.get("stock_number")
        counts["attempted"] += 1
        ctr_data = aggregated_ctr.get(stock)
        if not isinstance(ctr_data, dict) or ctr_data.get("error"):
            try:
                pr = acv.scrape_pricing(stock)
                ctr_data = acv.scrape_ctr(pr.get("vehicle_id"))
            except Exception as exc:  # noqa: BLE001 - one vehicle must never kill the run
                counts["failed"] += 1
                errors.append({"stock": stock, "phase": "ctr", "error": str(exc)})
                print(
                    f"[ctr] {i} of {total} — {stock}: scrape failed — {exc}",
                    file=sys.stderr,
                )
                continue
        if dry_run:
            print(
                f"[ctr] {i} of {total} — {stock}: would record "
                f"(AT {ctr_data.get('latest_autotrader_ctr')}, "
                f"CG {ctr_data.get('latest_cargurus_ctr')}, "
                f"avg {ctr_data.get('latest_average_ctr')})"
            )
            continue
        hist = ad_history.get(stock, {})
        try:
            record_ctr(
                v,
                ctr_data,
                ad_written=bool(hist),
                ad_written_date=hist.get("last_ad_date"),
            )
            counts["recorded"] += 1
            print(
                f"[ctr] {i} of {total} — {stock}: recorded "
                f"(AT {ctr_data.get('latest_autotrader_ctr')}, "
                f"CG {ctr_data.get('latest_cargurus_ctr')}, "
                f"avg {ctr_data.get('latest_average_ctr')})"
            )
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            errors.append({"stock": stock, "phase": "ctr_db", "error": str(exc)})
            print(
                f"[ctr] {i} of {total} — {stock}: db write failed — {exc}",
                file=sys.stderr,
            )
    return counts


def capture_benchmark_ctr(
    bx: ACVMaxScraper,
    *,
    dry_run: bool = False,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, int]]:
    """Northlake/Charlotte competitive benchmark CTR capture. `bx` must
    already be logged in (lands on Mercedes-Benz of Durham; each store switch
    is handled inside scrape_benchmark_inventory())."""
    if errors is None:
        errors = []
    results: dict[str, dict[str, int]] = {
        d: {"attempted": 0, "recorded": 0, "failed": 0} for d in BENCHMARK_DEALERSHIPS
    }
    for dealership_name in BENCHMARK_DEALERSHIPS:
        short = dealership_name.split()[-1]
        try:
            vehicles, failed = bx.scrape_benchmark_inventory(dealership_name)
        except Exception as exc:  # noqa: BLE001 - one store must not stop the other
            errors.append(
                {"stock": "-", "phase": "benchmark", "error": f"{dealership_name}: {exc}"}
            )
            print(f"[benchmark] {short} FAILED — {exc}", file=sys.stderr)
            continue

        n_total = len(vehicles) + len(failed)
        results[dealership_name]["attempted"] = n_total
        results[dealership_name]["failed"] = len(failed)
        for f in failed:
            errors.append(
                {
                    "stock": f.get("stock_number"),
                    "phase": "benchmark_ctr",
                    "error": f.get("error"),
                }
            )
            print(
                f"[benchmark] {short}: {f.get('stock_number')} — failed: {f.get('error')}",
                file=sys.stderr,
            )

        for i, veh in enumerate(vehicles, 1):
            tier = infer_tier(certified=veh.get("certified"), price=veh.get("current_price"))
            if dry_run:
                print(
                    f"[benchmark] {short}: {i} of {n_total} vehicles — "
                    f"would record {veh.get('stock_number')}"
                )
                continue
            try:
                record_ctr(
                    veh,
                    veh.get("ctr_data") or {},
                    dealership_name=dealership_name,
                    dealership_role="benchmark",
                    certification_tier=tier,
                )
                results[dealership_name]["recorded"] += 1
            except Exception as exc:  # noqa: BLE001
                results[dealership_name]["failed"] += 1
                errors.append(
                    {"stock": veh.get("stock_number"), "phase": "benchmark_db", "error": str(exc)}
                )
                continue
            print(
                f"[benchmark] {short}: {i} of {n_total} vehicles — "
                f"{veh.get('year_make_model') or '?'} {veh.get('stock_number') or '?'}"
            )
        print(
            f"[benchmark] {short} complete — "
            f"{results[dealership_name]['recorded']} recorded, "
            f"{results[dealership_name]['failed']} failed, of {n_total} attempted"
        )
    return results


def warmup(*, dry_run: bool = False) -> int:
    retail = _load_retail_vehicles()
    errors: list[dict[str, Any]] = []

    print(f"\n=== DURHAM CTR CAPTURE ({len(retail)} vehicle(s)) ===")
    with ACVMaxScraper(headless=True) as ax:
        ax.login()
        durham_counts = capture_durham_ctr(ax, retail, dry_run=dry_run, errors=errors)

    print("\n=== BENCHMARK CTR CAPTURE ===")
    with ACVMaxScraper(headless=True) as bx:
        bx.login()  # lands on Mercedes-Benz of Durham
        benchmark_counts = capture_benchmark_ctr(bx, dry_run=dry_run, errors=errors)

    print()
    print("=" * 50)
    print("CTR warmup complete" + (" (--dry-run, nothing written)" if dry_run else ""))
    print(f"  Durham:    {durham_counts['recorded']} recorded, {durham_counts['failed']} failed, of {durham_counts['attempted']} attempted")
    for name, c in benchmark_counts.items():
        short = name.split()[-1]
        print(f"  {short}: {c['recorded']} recorded, {c['failed']} failed, of {c['attempted']} attempted")
    if errors:
        print(f"\n{len(errors)} error(s):")
        for e in errors:
            print(f"  [{e.get('phase')}] {e.get('stock')}: {e.get('error')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture CTR snapshots for Durham retail + Northlake/Charlotte benchmark"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="scrape and log, but skip the DB write"
    )
    args = parser.parse_args(argv)
    return warmup(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
