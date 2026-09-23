#!/usr/bin/env python3
"""sticker_warmup.py — fetch real OEM window stickers for non-Mercedes-Benz
retail inventory that has none, or only ACV Max's approximate package list.

Reads last_inventory_snapshot.json. A vehicle is a target when it is not a
Mercedes-Benz AND its cached window_sticker_source in vehicle_cache.db is
missing, 'acvmax_options_tab' (approximate, non-OEM prices), or
'unavailable_after_retries'. Every other source is already a real sticker and
is skipped unconditionally; so is any VIN not in the snapshot (it may have
sold).

Per target, in order:
  1. Carfax sticker link — the window_sticker_url on the cached Carfax record,
     fetched and parsed by scraper._parse_oem_sticker() (which also keeps the
     raw PDF at sticker_cache/<VIN>.pdf).
  2. AutoiPacket — AutoiPacketScraper.pull_sticker(vin, non_mb=True) with
     every rate limit enforced (bypass_rate_limits=False). The 3-attempt
     autoipacket_attempts gate aggregate() applies is not checked here: the
     warmup always tries.
  3. Both fail — the cached row is left exactly as it was. Never falls back to
     the ACV Max options tab, and never overwrites a sticker with nothing.

A success is saved with save_window_sticker() and resets autoipacket_attempts
to 0. A single bad vehicle is logged and skipped — it never stops the run.
Holds orchestrator.lock for the whole run so it can't overlap an orchestrator
or verifier run.

    python3 sticker_warmup.py                 # all eligible non-MB vehicles
    python3 sticker_warmup.py --dry-run       # list targets; no fetches, no writes
    python3 sticker_warmup.py VIN [VIN ...]   # only these VINs
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aggregator import _make_from_ymm, _sticker_source
from run_lock import (
    ORCHESTRATOR_LOCK_PATH,
    ScraperBusyError,
    acquire_scraper_lock,
    release_lock_if_owned,
)
from scraper import (
    NON_MB_STICKER_MIN_DELAY_SECONDS,
    AutoiPacketScraper,
    _non_mb_sticker_last_pull_at,
    _parse_oem_sticker,
)
from vehicle_cache import _connect, get_vehicle, save_window_sticker

SNAPSHOT_PATH = Path(__file__).with_name("last_inventory_snapshot.json")

# Cached sticker sources the warmup will try to replace. None = no sticker.
# autoipacket_predictive is AutoiPacket's estimated build, not an OEM sticker,
# so like the ACV Max options tab it gets replaced when a real one turns up.
TARGET_SOURCES = {
    None, "acvmax_options_tab", "unavailable_after_retries", "autoipacket_predictive",
}
PREDICTIVE_SOURCE = "autoipacket_predictive"


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


def _usable(sticker: dict[str, Any] | None) -> bool:
    """Same bar aggregate() uses: no error, and a total MSRP or any package."""
    return bool(
        sticker
        and not sticker.get("error")
        and (sticker.get("total_msrp") is not None or sticker.get("option_packages"))
    )


def _reset_attempts(vin: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE vehicle_data SET autoipacket_attempts = 0, last_updated = ? WHERE vin = ?",
            (datetime.now().isoformat(), vin),
        )
        conn.commit()


def _is_predictive(sticker: dict[str, Any]) -> bool:
    """AutoiPacket's predictive build (watermarked PREDICTIVE DATA, totalled as
    TOTAL PREDICTED PRICE) rather than an OEM sticker. The result carries no
    flag for it, and its source only names the iPacket path that answered,
    so this reads the sticker text."""
    marker = f"{sticker.get('source') or ''} {sticker.get('raw_text') or ''}".upper()
    return "PREDICTIVE" in marker or "PREDICTED PRICE" in marker


def _spacing_wait_seconds(result: dict[str, Any] | None) -> int | None:
    """Seconds left on the non-MB 5-minute spacing when that, and only that,
    is why pull_sticker() was refused; None for any other outcome (daily caps,
    the 11am-6pm window, success, a real failure)."""
    if not (result and result.get("rate_limited")):
        return None
    if f"less than {NON_MB_STICKER_MIN_DELAY_SECONDS}s since the last non-MB pull" not in (result.get("error") or ""):
        return None
    last = _non_mb_sticker_last_pull_at()
    if last is None:
        return 0
    ready = last + timedelta(seconds=NON_MB_STICKER_MIN_DELAY_SECONDS)
    return max(0, int((ready - datetime.now()).total_seconds()) + 1)


def _summary(sticker: dict[str, Any]) -> str:
    total = sticker.get("total_msrp")
    total_str = f"MSRP ${total:,.0f}" if total is not None else "no total MSRP"
    return f"{total_str}, {len(sticker.get('option_packages') or [])} package(s)"


def _select(
    vehicles: list[dict[str, Any]], only_vins: list[str]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any] | None, str]], dict[str, int]]:
    """(targets as (snapshot vehicle, cached row, make), skip counts)."""
    skips = {"real_sticker": 0, "mercedes": 0, "not_in_snapshot": 0, "unknown_make": 0}
    by_vin = {(v.get("vin") or "").strip().upper(): v for v in vehicles if v.get("vin")}
    if only_vins:
        wanted = [x.strip().upper() for x in only_vins]
        for vin in wanted:
            if vin not in by_vin:
                skips["not_in_snapshot"] += 1
                print(f"[sticker_warmup] {vin} — skipped (not in {SNAPSHOT_PATH.name}, may have sold)")
        candidates = [by_vin[v] for v in wanted if v in by_vin]
    else:
        candidates = list(by_vin.values())

    targets = []
    for v in candidates:
        vin = v["vin"].strip().upper()
        stock = v.get("stock_number") or "?"
        row = get_vehicle(vin)
        ymm = v.get("year_make_model") or (row or {}).get("year_make_model")
        make = _make_from_ymm(ymm)
        if not make:
            skips["unknown_make"] += 1
            print(f"[sticker_warmup] {stock} {vin} — skipped (make unknown: {ymm!r})")
            continue
        if "mercedes" in make.lower():
            skips["mercedes"] += 1
            continue
        source = (row or {}).get("window_sticker_source") if (row or {}).get("window_sticker_json") else None
        if source not in TARGET_SOURCES:
            skips["real_sticker"] += 1
            print(f"[sticker_warmup] {stock} {ymm} — skipped (already has real sticker: {source})")
            continue
        targets.append((v, row, make))
    return targets, skips


def warmup(*, only_vins: list[str], dry_run: bool, headless: bool = True) -> int:
    started = time.monotonic()
    targets, skips = _select(_load_retail_vehicles(), only_vins)
    total = len(targets)
    counts = {"upgraded": 0, "new": 0, "predictive": 0, "failed": 0}

    print(f"\n[sticker_warmup] {total} target vehicle(s)" + (" — DRY RUN" if dry_run else ""))
    if dry_run:
        for i, (v, row, make) in enumerate(targets, 1):
            cf = json.loads((row or {}).get("carfax_json") or "{}")
            source = (row or {}).get("window_sticker_source") if (row or {}).get("window_sticker_json") else None
            print(
                f"[sticker_warmup] {i} of {total} — {v.get('stock_number')} "
                f"{v.get('year_make_model')} ({v['vin']}) — current: {source or 'none'} — "
                f"would try: {'Carfax link, then ' if cf.get('window_sticker_url') else ''}iPacket"
            )
    elif targets:
        try:
            acquire_scraper_lock(ORCHESTRATOR_LOCK_PATH, wait_seconds=0)
        except ScraperBusyError as exc:
            print(f"[sticker_warmup] orchestrator/verifier run in progress — {exc}", file=sys.stderr)
            return 1
        try:
            _run(targets, counts, headless=headless)
        finally:
            release_lock_if_owned(ORCHESTRATOR_LOCK_PATH)

    elapsed = time.monotonic() - started
    print()
    print("=" * 50)
    print("Sticker warmup complete" + (" (dry run — nothing fetched or written)" if dry_run else ""))
    print(f"  Already have real sticker (skipped):  {skips['real_sticker']}")
    print(f"  Mercedes-Benz (not in scope):          {skips['mercedes']}")
    print(f"  Not in snapshot (skipped):             {skips['not_in_snapshot']}")
    if skips["unknown_make"]:
        print(f"  Make unknown (skipped):                {skips['unknown_make']}")
    print(f"  Targets:                               {total}")
    print(f"  Upgraded from ACV Max:                 {counts['upgraded']}")
    print(f"  Newly fetched:                         {counts['new']}")
    print(f"    of which predictive (not OEM):       {counts['predictive']}")
    print(f"  Failed both paths (left untouched):    {counts['failed']}")
    print(f"  Total runtime:                         {elapsed:.0f}s")
    return 0


def _run(
    targets: list[tuple[dict[str, Any], dict[str, Any] | None, str]],
    counts: dict[str, int],
    *,
    headless: bool,
) -> None:
    total = len(targets)
    ipacket: AutoiPacketScraper | None = None
    try:
        for i, (v, row, make) in enumerate(targets, 1):
            vin = v["vin"].strip().upper()
            stock = v.get("stock_number")
            ymm = v.get("year_make_model") or (row or {}).get("year_make_model")
            prior = (row or {}).get("window_sticker_source") if (row or {}).get("window_sticker_json") else None
            tag = f"[sticker_warmup] {i} of {total} — {stock} {ymm}"
            sticker = source = None

            # 1. Carfax sticker link
            cf = json.loads((row or {}).get("carfax_json") or "{}")
            url = cf.get("window_sticker_url")
            if url:
                try:
                    data = _parse_oem_sticker(url, vin, make)
                except Exception as exc:  # noqa: BLE001 - one vehicle must never kill the run
                    print(f"{tag} — Carfax link failed: {exc}")
                else:
                    if _usable(data):
                        sticker, source = data, "carfax_sticker_link"
                    else:
                        print(f"{tag} — Carfax link parsed but unusable")
            else:
                print(f"{tag} — no Carfax sticker link on record")

            # 2. AutoiPacket (rate limits enforced)
            if sticker is None:
                try:
                    if ipacket is None:
                        ipacket = AutoiPacketScraper(headless=headless, use_saved_session=True).__enter__()
                        ipacket.login()
                    data = ipacket.pull_sticker(vin, bypass_rate_limits=False, non_mb=True)
                    wait = _spacing_wait_seconds(data)
                    if wait is not None:
                        # Only the 5-minute spacing gets a retry; daily caps
                        # and the time window fail straight through below.
                        print(f"[warmup] {vin} — 5-minute gap, waiting {wait}s then retrying")
                        time.sleep(wait + 5)
                        data = ipacket.pull_sticker(vin, bypass_rate_limits=False, non_mb=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"{tag} — iPacket failed: {exc}")
                else:
                    if _usable(data):
                        sticker = data
                        source = PREDICTIVE_SOURCE if _is_predictive(data) else _sticker_source(data)
                    elif data and data.get("rate_limited"):
                        print(f"{tag} — iPacket held back by rate limit: {data.get('error')}")
                    else:
                        print(f"{tag} — iPacket returned nothing usable")

            # 3. Both failed: leave the row untouched
            if sticker is None:
                counts["failed"] += 1
                print(f"{tag} — FAILED both paths, cached data left untouched ({prior or 'no sticker'})")
                continue

            save_window_sticker(
                vin, stock, ymm, sticker, source,
                image_path=sticker.get("sticker_image_path"),
            )
            _reset_attempts(vin)
            counts["upgraded" if prior == "acvmax_options_tab" else "new"] += 1
            if source == PREDICTIVE_SOURCE:
                counts["predictive"] += 1
            print(
                f"{tag} — saved via {source} ({_summary(sticker)}); "
                f"DB updated, was {prior or 'no sticker'}; retry counter reset"
            )
    finally:
        if ipacket is not None:
            ipacket.__exit__(None, None, None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch real OEM window stickers for non-MB inventory"
    )
    parser.add_argument("vins", nargs="*", help="only these VINs (default: every eligible vehicle)")
    parser.add_argument(
        "--dry-run", action="store_true", help="show targets only; no fetches, no DB writes"
    )
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args(argv)
    return warmup(only_vins=args.vins, dry_run=args.dry_run, headless=not args.headed)


if __name__ == "__main__":
    raise SystemExit(main())
