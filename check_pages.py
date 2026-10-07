#!/usr/bin/env python3
"""check_pages.py — render every page through Flask's test client and report
any non-200. Run after a template or app.py change:

    python check_pages.py

Covers the main pages (/, /inventory, /cache, /ctr, /cost, /about) plus /cache/<stock>
for every snapshot stock and every cached row with a stock number, and
/inventory/<stock> for every snapshot stock. Read-only: GETs only, with a
test-client session marked authed (nothing is sent to any live service).
Exits 1 if anything fails."""
from __future__ import annotations

import logging
import sqlite3
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import app as adapp  # noqa: E402

MAIN_PAGES = ["/", "/inventory", "/cache", "/ctr", "/cost", "/about"]


def _stocks() -> tuple[list[str], list[str]]:
    vehicles, _stamp = adapp._load_snapshot()
    snap = {adapp.normalize_stock(v.get("stock_number")) for v in vehicles if v.get("stock_number")}
    conn = sqlite3.connect(str(ROOT / "vehicle_cache.db"))
    try:
        cached = {
            adapp.normalize_stock(r[0])
            for r in conn.execute(
                "SELECT stock_number FROM vehicle_data "
                "WHERE stock_number IS NOT NULL AND stock_number != ''"
            )
        }
    finally:
        conn.close()
    return sorted(snap), sorted(snap | cached)


def main() -> int:
    logging.disable(logging.CRITICAL)
    adapp.app.config["TESTING"] = True
    adapp.app.config["PROPAGATE_EXCEPTIONS"] = True
    client = adapp.app.test_client()
    with client.session_transaction() as sess:
        sess["authed"] = True

    snap, cache_stocks = _stocks()
    paths = (
        MAIN_PAGES
        + [f"/inventory/{s}" for s in snap]
        + [f"/cache/{s}" for s in cache_stocks]
    )
    failures: list[tuple[str, str]] = []
    for path in paths:
        try:
            status = client.get(path).status_code
            if status != 200:
                failures.append((path, f"HTTP {status}"))
        except Exception as exc:  # noqa: BLE001
            tb = traceback.extract_tb(exc.__traceback__)
            where = next(
                (f"{Path(f.filename).name}:{f.lineno}" for f in reversed(tb) if "templates" in f.filename),
                f"{Path(tb[-1].filename).name}:{tb[-1].lineno}",
            )
            failures.append((path, f"{type(exc).__name__}: {str(exc)[:100]} ({where})"))

    print(f"checked {len(paths)} pages: {len(paths) - len(failures)} ok, {len(failures)} failed")
    for path, why in failures:
        print(f"  FAIL {path}  {why}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
