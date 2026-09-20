#!/usr/bin/env python3
# MANUAL USE ONLY — never call from orchestrator or scheduled tasks
"""vin_importer.py — bulk-pull AutoiPacket window stickers for a CSV of VINs.

Reads a Market Listings style CSV, pulls the OEM window sticker for every VIN via
AutoiPacketScraper, and stores the structured equipment data in rarity.db so it
can be mined later for equipment-rarity analysis.

    python3 vin_importer.py MarketListings.csv

Re-runnable: VINs already stored as `complete` or `not_covered` are skipped;
`failed` and `pending` VINs are retried on the next run.
"""

from __future__ import annotations

import argparse
import csv
import inspect
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from scraper import (
    AutoiPacketScraper,
    ScraperError,
    StickerNotFoundError,
)

DB_PATH = Path(__file__).with_name("rarity.db")
REQUEST_DELAY_SECONDS = 3

# Filenames whose presence anywhere in the call stack marks this as an
# automated/scheduled invocation rather than a manual one.
_AUTOMATED_CALLER_FILENAMES = {"orchestrator.py"}
# An automation wrapper (scheduler, cron shim, etc.) can also self-identify by
# setting this env var, since not every automated context runs through
# orchestrator.py.
_AUTOMATED_ENV_FLAG = "ADWRITER_AUTOMATED"


def _reject_if_automated() -> None:
    """Refuse to proceed if this bulk import was triggered by orchestrator.py
    or any other automated/scheduled context."""
    if os.environ.get(_AUTOMATED_ENV_FLAG):
        raise RuntimeError(
            "vin_importer must be run manually only — automated bulk imports "
            "are disabled"
        )
    for frame_info in inspect.stack():
        if Path(frame_info.filename).name.lower() in _AUTOMATED_CALLER_FILENAMES:
            raise RuntimeError(
                "vin_importer must be run manually only — automated bulk imports "
                "are disabled"
            )

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_equipment (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    vin               TEXT UNIQUE NOT NULL,
    year              INTEGER,
    make              TEXT,
    model             TEXT,
    trim              TEXT,
    exterior_color    TEXT,
    mileage           REAL,
    certified         INTEGER,
    price             REAL,
    base_price        REAL,
    total_msrp        REAL,
    freight           REAL,
    option_packages   TEXT,   -- JSON
    standard_options  TEXT,   -- JSON
    raw_sticker_text  TEXT,
    source            TEXT,
    import_date       TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending/complete/failed/not_covered/unavailable
    retry_count       INTEGER NOT NULL DEFAULT 0,       -- attempts made so far
    error             TEXT
);
CREATE INDEX IF NOT EXISTS ix_vehicle_equipment_status
    ON vehicle_equipment (status);
"""

# Columns added after the first release — ALTERed onto pre-existing databases.
_MIGRATIONS = [
    (
        "retry_count",
        "ALTER TABLE vehicle_equipment ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
        # the bulk run already made one attempt on every not_covered VIN
        "UPDATE vehicle_equipment SET retry_count = 1 WHERE status = 'not_covered'",
    ),
]

# A not_covered VIN is retried until it has been attempted this many times;
# after that it is permanently marked 'unavailable'.
MAX_NOT_COVERED_ATTEMPTS = 2

# Makes whose name is two whitespace-separated words.
_TWO_WORD_MAKE_FIRST = {"land", "alfa", "aston", "rolls"}
_TRUTHY = {"y", "yes", "true", "1", "t", "certified", "cpo"}


# --------------------------------------------------------------------------- #
# CSV parsing
# --------------------------------------------------------------------------- #


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    m = re.search(r"-?\$?\s*([\d,]+(?:\.\d+)?)", str(text))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_description(desc: str | None) -> tuple[int | None, str | None, str | None, str | None]:
    """'2023 Mercedes-Benz GLC 300 4MATIC' -> (2023, 'Mercedes-Benz', 'GLC', '300 4MATIC')."""
    if not desc:
        return None, None, None, None
    tokens = str(desc).split()
    year: int | None = None
    if tokens and re.fullmatch(r"(19|20)\d{2}", tokens[0]):
        year = int(tokens[0])
        tokens = tokens[1:]
    if not tokens:
        return year, None, None, None
    if tokens[0].lower() in _TWO_WORD_MAKE_FIRST and len(tokens) > 1:
        make = f"{tokens[0]} {tokens[1]}"
        rest = tokens[2:]
    else:
        make = tokens[0]
        rest = tokens[1:]
    model = rest[0] if rest else None
    trim = " ".join(rest[1:]) if len(rest) > 1 else None
    return year, make, model, trim


def is_mercedes_make(make: str | None) -> bool:
    """Single source of truth for "is this vehicle a Mercedes-Benz" by its parsed
    make. rarity.db is Mercedes-Benz only; every writer that can put a row in it
    (aggregator._capture_sticker_to_rarity(), run_import()) calls this rather
    than carrying its own copy of the check. A missing make is not Mercedes."""
    return bool(make) and "mercedes" in str(make).lower()


def _get(row: dict[str, str], *names: str) -> str | None:
    """Case-insensitive column lookup, first match wins."""
    lowered = {k.lower().strip(): v for k, v in row.items() if k}
    for n in names:
        v = lowered.get(n.lower())
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def read_csv(path: Path) -> list[dict[str, Any]]:
    """Return one context dict per unique VIN in the CSV (first occurrence wins)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            vin = _get(row, "vin")
            if not vin:
                continue
            vin = vin.strip().upper()
            if vin in seen:
                continue
            seen.add(vin)
            year, make, model, trim = _parse_description(
                _get(row, "vehicledescription", "vehicle_description", "description")
            )
            certified_raw = _get(row, "certified", "certifiedstatus", "cpo")
            out.append(
                {
                    "vin": vin,
                    "year": year,
                    "make": make,
                    "model": model,
                    "trim": trim,
                    "exterior_color": _get(row, "color", "exteriorcolor", "exterior_color"),
                    "mileage": _num(_get(row, "mileage", "odometer", "miles")),
                    "certified": (
                        1
                        if certified_raw and certified_raw.strip().lower() in _TRUTHY
                        else (0 if certified_raw is not None else None)
                    ),
                    "price": _num(_get(row, "internetprice", "internet_price", "price")),
                }
            )
    return out


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(vehicle_equipment)")}
    for col, alter_sql, backfill_sql in _MIGRATIONS:
        if col not in have:
            conn.execute(alter_sql)
            if backfill_sql:
                conn.execute(backfill_sql)
    conn.commit()
    return conn


def _upsert_context(conn: sqlite3.Connection, ctx: dict[str, Any]) -> None:
    """Insert a pending row for a new VIN, or refresh the CSV-context fields on
    an existing non-complete row. Never downgrades a status."""
    conn.execute(
        """
        INSERT INTO vehicle_equipment
            (vin, year, make, model, trim, exterior_color, mileage, certified,
             price, import_date, status)
        VALUES (:vin, :year, :make, :model, :trim, :exterior_color, :mileage,
                :certified, :price, :import_date, 'pending')
        ON CONFLICT(vin) DO UPDATE SET
            year = excluded.year,
            make = excluded.make,
            model = excluded.model,
            trim = excluded.trim,
            exterior_color = excluded.exterior_color,
            mileage = excluded.mileage,
            certified = excluded.certified,
            price = excluded.price
        WHERE vehicle_equipment.status IN ('pending', 'failed', 'not_covered')
        """,
        {**ctx, "import_date": date.today().isoformat()},
    )
    conn.commit()


def _vin_state(conn: sqlite3.Connection, vin: str) -> tuple[str | None, int]:
    """(status, retry_count) for a VIN, or (None, 0) if not in the DB yet."""
    row = conn.execute(
        "SELECT status, retry_count FROM vehicle_equipment WHERE vin = ?", (vin,)
    ).fetchone()
    return (row["status"], row["retry_count"]) if row else (None, 0)


def _bump_retry(conn: sqlite3.Connection, vin: str) -> None:
    conn.execute(
        "UPDATE vehicle_equipment SET retry_count = retry_count + 1 WHERE vin = ?",
        (vin,),
    )
    conn.commit()


def _store_complete(
    conn: sqlite3.Connection,
    vin: str,
    data: dict[str, Any],
    source: str | None = None,
) -> None:
    resolved_source = source or {
        "html": "autoipacket_html",
        "pdf": "autoipacket_pdf",
    }.get(data.get("render"), "autoipacket")
    conn.execute(
        """
        UPDATE vehicle_equipment SET
            base_price = ?, total_msrp = ?, freight = ?,
            option_packages = ?, standard_options = ?, raw_sticker_text = ?,
            source = ?, status = 'complete', error = NULL
        WHERE vin = ?
        """,
        (
            data.get("base_price"),
            data.get("total_msrp"),
            data.get("freight"),
            json.dumps(data.get("option_packages") or []),
            json.dumps(data.get("standard_options") or []),
            data.get("raw_text"),
            resolved_source,
            vin,
        ),
    )
    conn.commit()


def _set_status(
    conn: sqlite3.Connection, vin: str, status: str, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE vehicle_equipment SET status = ?, error = ? WHERE vin = ?",
        (status, error, vin),
    )
    conn.commit()


def is_captured(vin: str) -> bool:
    """True if this VIN is already in rarity.db with status 'complete'."""
    vin = (vin or "").strip().upper()
    if not vin:
        return False
    conn = _connect()
    try:
        status, _ = _vin_state(conn, vin)
        return status == "complete"
    finally:
        conn.close()


def capture_sticker(
    vin: str,
    sticker_data: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    source: str = "ad_write_capture",
) -> bool:
    """Store a one-off window-sticker pull in rarity.db, using the same schema as
    the bulk importer. No-op (returns False) if the VIN is already 'complete';
    otherwise writes the sticker data, marks the row 'complete', and returns True.

    `sticker_data` is an AutoiPacketScraper.pull_sticker() return.
    `context` may carry year/make/model/trim/exterior_color/mileage/certified/price.
    Intended for aggregator.aggregate() to grow the equipment corpus during ad
    writes.
    """
    vin = (vin or "").strip().upper()
    if not vin or not sticker_data or sticker_data.get("error"):
        return False
    # pull_sticker() can "succeed" (no error key) but still carry nothing usable
    # for a VIN it doesn't cover — never cache that as 'complete', or it
    # permanently poisons the tier-1 rarity.db cache with an empty row and blocks
    # every future real capture (including a later Carfax-sticker-link recovery).
    if sticker_data.get("total_msrp") is None and not sticker_data.get("option_packages"):
        return False

    conn = _connect()
    try:
        status, _ = _vin_state(conn, vin)
        if status == "complete":
            return False

        ctx = {
            "vin": vin,
            "year": None,
            "make": None,
            "model": None,
            "trim": None,
            "exterior_color": None,
            "mileage": None,
            "certified": None,
            "price": None,
            **(context or {}),
        }
        ctx["vin"] = vin  # context must never override the key column
        _upsert_context(conn, ctx)  # inserts a pending row or refreshes context
        _store_complete(conn, vin, sticker_data, source=source)
        return True
    finally:
        conn.close()


def get_sticker_from_db(vin: str) -> dict[str, Any] | None:
    """Tier-1 cache read for AutoiPacketScraper.pull_sticker(): a stored
    'complete' sticker for this VIN, shaped like a pull_sticker() return, or
    None. No browser involved."""
    vin = (vin or "").strip().upper()
    if not vin:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM vehicle_equipment WHERE vin = ? AND status = 'complete'",
            (vin,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None

    def _loads(raw: Any) -> list:
        try:
            v = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            return []
        return v if isinstance(v, list) else []

    ymm = " ".join(
        str(x) for x in (row["year"], row["make"], row["model"], row["trim"]) if x
    ) or None
    render = "pdf" if row["source"] == "autoipacket_pdf" else "html"
    return {
        "source": "rarity_db_cache",
        "vin": vin,
        "year_make_model": ymm,
        "exterior_color": row["exterior_color"],
        "interior_color": None,
        "base_price": row["base_price"],
        "freight": row["freight"],
        "total_msrp": row["total_msrp"],
        "option_packages": _loads(row["option_packages"]),
        "added_options_all": [],
        "standard_options": _loads(row["standard_options"]),
        "raw_text": row["raw_sticker_text"] or "",
        "render": render,
        "sticker_url": None,
        "stored_source": row["source"],
        "cached_import_date": row["import_date"],
    }


def mark_failed(
    vin: str, error: Any, context: dict[str, Any] | None = None
) -> None:
    """Record a scrape failure for a VIN in rarity.db (status 'failed'), creating
    the row if needed. Never downgrades a row that is already 'complete'."""
    vin = (vin or "").strip().upper()
    if not vin:
        return
    conn = _connect()
    try:
        status, _ = _vin_state(conn, vin)
        if status == "complete":
            return
        ctx = {
            "vin": vin,
            "year": None,
            "make": None,
            "model": None,
            "trim": None,
            "exterior_color": None,
            "mileage": None,
            "certified": None,
            "price": None,
            **(context or {}),
        }
        ctx["vin"] = vin
        _upsert_context(conn, ctx)
        _set_status(conn, vin, "failed", str(error)[:2000])
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Import run
# --------------------------------------------------------------------------- #


def run_import(csv_path: Path) -> int:
    _reject_if_automated()
    rows = read_csv(csv_path)
    total = len(rows)
    if total == 0:
        print(f"No VINs found in {csv_path} (expected a 'VIN' column).", file=sys.stderr)
        return 1
    print(f"Loaded {total} unique VIN(s) from {csv_path.name}")

    conn = _connect()
    counts = {
        "complete": 0,
        "not_covered": 0,
        "unavailable": 0,
        "failed": 0,
        "skipped": 0,
    }

    with AutoiPacketScraper(headless=True) as scraper:
        scraper.login()

        for i, ctx in enumerate(rows, start=1):
            vin = ctx["vin"]

            # rarity.db is Mercedes-Benz only: never pull or write a row for
            # any other make, before anything touches the database.
            if not is_mercedes_make(ctx.get("make")):
                counts["skipped"] += 1
                print(
                    f"Processing VIN {i} of {total} — {vin} — skipped "
                    f"(non-Mercedes make {ctx.get('make')!r})"
                )
                continue

            existing, retries = _vin_state(conn, vin)

            # Terminal states: done, or not_covered after all its retries.
            terminal = existing in ("complete", "unavailable") or (
                existing == "not_covered" and retries >= MAX_NOT_COVERED_ATTEMPTS
            )
            if terminal:
                counts["skipped"] += 1
                print(
                    f"Processing VIN {i} of {total} — {vin} — skipped "
                    f"({existing})"
                )
                continue

            _upsert_context(conn, ctx)
            _bump_retry(conn, vin)
            attempts = retries + 1

            try:
                # bulk import goes straight to the endpoint, not the
                # cache/browse tiers of the smart pull_sticker()
                data = scraper.pull_sticker_endpoint(vin)
                if data is None:
                    # Blocked by the endpoint's own rate limiter (outside
                    # business hours, or today's daily cap already reached).
                    # Not a real attempt — leave status/retry_count as-is and
                    # stop the run, since the same gate will block every
                    # remaining VIN too.
                    print(
                        f"Processing VIN {i} of {total} — {vin} — stopping "
                        f"(iPacket sticker pull rate-limited)"
                    )
                    break
                _store_complete(conn, vin, data)
                outcome = "complete"
                counts["complete"] += 1
            except StickerNotFoundError as exc:
                if attempts >= MAX_NOT_COVERED_ATTEMPTS:
                    _set_status(conn, vin, "unavailable", str(exc))
                    outcome = "unavailable"
                    counts["unavailable"] += 1
                else:
                    _set_status(conn, vin, "not_covered", str(exc))
                    outcome = f"not_covered (attempt {attempts}/{MAX_NOT_COVERED_ATTEMPTS})"
                    counts["not_covered"] += 1
            except Exception as exc:  # noqa: BLE001 - any other failure is retryable
                _set_status(conn, vin, "failed", str(exc))
                outcome = "failed"
                counts["failed"] += 1
                print(f"    ! {vin} failed: {exc}", file=sys.stderr)

            print(f"Processing VIN {i} of {total} — {vin} — {outcome}")
            time.sleep(REQUEST_DELAY_SECONDS)

    conn.close()

    print()
    print("=" * 50)
    print("IMPORT SUMMARY")
    print("=" * 50)
    print(f"  Complete:               {counts['complete']}")
    print(f"  Not covered (retrying): {counts['not_covered']}")
    print(f"  Unavailable (permanent):{counts['unavailable']}")
    print(f"  Failed:                 {counts['failed']}")
    print(f"  Skipped:                {counts['skipped']}")
    print(f"  Total VINs:             {total}")
    print(f"  Database:               {DB_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bulk-pull AutoiPacket window stickers for a CSV of VINs into rarity.db"
    )
    parser.add_argument("csv", help="path to the Market Listings CSV (needs a 'VIN' column)")
    args = parser.parse_args(argv)

    csv_path = Path(args.csv)
    if not csv_path.is_file():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 1

    try:
        return run_import(csv_path)
    except ScraperError as exc:
        print(f"ERROR: AutoiPacket login/setup failed — {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
