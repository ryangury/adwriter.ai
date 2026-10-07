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

from ctr_database import (
    BENCHMARK_DEALERSHIPS,
    infer_mileage_tier,
    record_ctr,
)
from failure_streak import FailureStreak
from scraper import AcvMaxRunAbort, ACVMaxScraper, ScraperError

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
    streak: FailureStreak | None = None,
    skip: set[str] | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Durham retail CTR capture for every vehicle in `retail`. `acv` must
    already be logged in. aggregated_ctr lets a caller that already scraped a
    vehicle's CTR this run (orchestrator.py, during ad generation) skip a
    redundant scrape for it; ctr_warmup.py's own standalone run passes none,
    so every vehicle gets a fresh scrape.

    With a `streak`, the loop stops after that many identical scrape failures
    in a row and returns the repeated message as counts["aborted"]; the
    caller alerts. counts["aborted"] is None when the loop ran to the end.

    Watchdog hooks (ctr_child.py): `skip` stocks are not read (they overran the
    per-vehicle limit on an earlier attempt); `progress` (a
    step_watchdog.ProgressWriter) gets a start / done per vehicle."""
    ad_history = ad_history or {}
    aggregated_ctr = aggregated_ctr or {}
    if errors is None:
        errors = []
    counts: dict[str, Any] = {"attempted": 0, "recorded": 0, "failed": 0, "aborted": None}
    total = len(retail)
    for i, v in enumerate(retail, 1):
        stock = v.get("stock_number")
        counts["attempted"] += 1
        if skip and stock in skip:
            counts["failed"] += 1
            errors.append({"stock": stock, "phase": "ctr", "error": "skipped: over the per-vehicle time limit"})
            print(f"[ctr] {i} of {total} — {stock}: skipped (over the per-vehicle time limit on an earlier attempt)")
            continue
        if progress is not None:
            progress.start(stock)
        ctr_data = aggregated_ctr.get(stock)
        if not isinstance(ctr_data, dict) or ctr_data.get("error"):
            try:
                pr = acv.scrape_pricing(stock)
                ctr_data = acv.scrape_ctr(pr.get("vehicle_id"))
            except AcvMaxRunAbort:
                raise  # ACV MAX itself is unusable - stop, don't fail every vehicle
            except Exception as exc:  # noqa: BLE001 - one vehicle must never kill the run
                if progress is not None:
                    progress.done()
                counts["failed"] += 1
                errors.append({"stock": stock, "phase": "ctr", "error": str(exc)})
                print(
                    f"[ctr] {i} of {total} — {stock}: scrape failed — {exc}",
                    file=sys.stderr,
                )
                if streak is not None and streak.fail(str(exc)):
                    counts["aborted"] = streak.reason
                    print(
                        f"[ctr] stopping the CTR loop: the same failure "
                        f"{streak.count} times in a row — {streak.reason}",
                        file=sys.stderr,
                    )
                    break
                continue
        if streak is not None:
            streak.ok()
        if progress is not None:
            progress.done()
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
    stores: list[str] | None = None,
    skip: set[str] | None = None,
    progress: Any = None,
) -> dict[str, dict[str, int]]:
    """Northlake/Charlotte competitive benchmark CTR capture. `bx` must
    already be logged in (lands on Mercedes-Benz of Durham; each store switch
    is handled inside scrape_benchmark_inventory()). Each vehicle is recorded
    as soon as its CTR is read, so a hang later in the store loses only the
    vehicle in flight. stores: default BENCHMARK_DEALERSHIPS; skip / progress:
    the watchdog hooks (ctr_child.py)."""
    if errors is None:
        errors = []
    stores = stores or list(BENCHMARK_DEALERSHIPS)
    results: dict[str, dict[str, int]] = {d: {"attempted": 0, "recorded": 0, "failed": 0} for d in stores}
    for dealership_name in stores:
        short = dealership_name.split()[-1]
        res = results[dealership_name]

        def on_result(veh: dict[str, Any], ok: bool, *, _name=dealership_name, _short=short, _res=res) -> None:
            _res["attempted"] += 1
            if not ok:
                _res["failed"] += 1
                errors.append({"stock": veh.get("stock_number"), "phase": "benchmark_ctr", "error": veh.get("error")})
                print(f"[benchmark] {_short}: {veh.get('stock_number')} — failed: {veh.get('error')}", file=sys.stderr)
                return
            # Both benchmark stores: tier from the row's real status code,
            # objective and mileage (see infer_mileage_tier).
            tier = infer_mileage_tier(
                veh.get("status_code"), veh.get("objective"), veh.get("mileage"), veh.get("year_make_model"),
            )
            if dry_run:
                print(f"[benchmark] {_short}: {_res['attempted']} — would record {veh.get('stock_number')}")
                return
            try:
                record_ctr(
                    veh,
                    veh.get("ctr_data") or {},
                    dealership_name=_name,
                    dealership_role="benchmark",
                    certification_tier=tier,
                    status_code=veh.get("status_code"),
                    mileage=veh.get("mileage"),
                    objective=veh.get("objective"),
                )
                _res["recorded"] += 1
            except Exception as exc:  # noqa: BLE001
                _res["failed"] += 1
                errors.append({"stock": veh.get("stock_number"), "phase": "benchmark_db", "error": str(exc)})
                return
            print(
                f"[benchmark] {_short}: {_res['attempted']} vehicles — "
                f"{veh.get('year_make_model') or '?'} {veh.get('stock_number') or '?'}"
            )

        try:
            bx.scrape_benchmark_inventory(dealership_name, skip=skip, progress=progress, on_result=on_result)
        except AcvMaxRunAbort:
            raise
        except Exception as exc:  # noqa: BLE001 - one store must not stop the other
            errors.append(
                {"stock": "-", "phase": "benchmark", "error": f"{dealership_name}: {exc}"}
            )
            print(f"[benchmark] {short} FAILED — {exc}", file=sys.stderr)
            continue
        print(
            f"[benchmark] {short} complete — "
            f"{res['recorded']} recorded, {res['failed']} failed, of {res['attempted']} attempted"
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
        bx.require_durham("after the Northlake/Charlotte benchmark")

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
