#!/usr/bin/env python3
"""vehicle_cache.py — local cache for stable per-vehicle data, keyed on VIN.

One SQLite table in vehicle_cache.db:

  vehicle_data — window sticker, Carfax, and recon data per VIN, each with its
                 own cached-date and its own freshness rule (see TTL constants
                 below), so we never re-scrape data that hasn't changed.

    from vehicle_cache import (
        get_vehicle, get_window_sticker, get_carfax, get_recon,
        save_window_sticker, save_carfax, save_recon,
    )
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).with_name("vehicle_cache.db")

# Freshness rules, in days. None means "cache forever" — never re-scrape.
WINDOW_STICKER_TTL: int | None = None  # window stickers never change
CARFAX_TTL: int | None = 14            # Carfax updates occasionally
RECON_TTL: int | None = None           # once recon_complete, it never changes

CACHE_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_data (
    vin                     TEXT PRIMARY KEY,
    stock_number            TEXT,
    year_make_model         TEXT,
    window_sticker_json     TEXT,
    window_sticker_date     TEXT,
    window_sticker_source   TEXT,
    sticker_image_path      TEXT,
    carfax_json             TEXT,
    carfax_date             TEXT,
    carfax_image_path       TEXT,
    recon_json              TEXT,
    recon_date              TEXT,
    recon_image_path        TEXT,
    recon_complete          INTEGER NOT NULL DEFAULT 0,
    autoipacket_attempts    INTEGER NOT NULL DEFAULT 0,
    seller_comments         TEXT,
    cache_version           INTEGER NOT NULL DEFAULT 1,
    created_date            TEXT,
    last_updated            TEXT
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database's first CREATE TABLE — CREATE
    TABLE IF NOT EXISTS is a no-op against an existing table, so a new column
    needs its own ALTER TABLE against any vehicle_cache.db created before it
    existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(vehicle_data)")}
    if "autoipacket_attempts" not in cols:
        conn.execute(
            "ALTER TABLE vehicle_data ADD COLUMN autoipacket_attempts "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if "carfax_image_path" not in cols:
        conn.execute("ALTER TABLE vehicle_data ADD COLUMN carfax_image_path TEXT")
    if "sticker_image_path" not in cols:
        conn.execute("ALTER TABLE vehicle_data ADD COLUMN sticker_image_path TEXT")
    if "recon_image_path" not in cols:
        conn.execute("ALTER TABLE vehicle_data ADD COLUMN recon_image_path TEXT")
    if "seller_comments" not in cols:
        conn.execute("ALTER TABLE vehicle_data ADD COLUMN seller_comments TEXT")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _days_old(iso_date: str | None) -> int | None:
    """Days between iso_date and today, or None if iso_date is missing/unparseable."""
    if not iso_date:
        return None
    try:
        return (date.today() - date.fromisoformat(iso_date)).days
    except ValueError:
        return None


def _is_fresh(iso_date: str | None, ttl_days: int | None) -> bool:
    """True if data cached on iso_date is still fresh under ttl_days.
    ttl_days=None means cache forever (fresh as long as it exists)."""
    if not iso_date:
        return False
    if ttl_days is None:
        return True
    age = _days_old(iso_date)
    return age is not None and age <= ttl_days


def _get_row(vin: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM vehicle_data WHERE vin = ? LIMIT 1", (vin,)
        ).fetchone()


def _ensure_row(conn: sqlite3.Connection, vin: str, stock_number: str | None) -> None:
    """Make sure a row exists for this VIN before an UPDATE-style upsert."""
    now = datetime.now().isoformat()
    conn.execute(
        """
        INSERT INTO vehicle_data (vin, stock_number, cache_version, created_date, last_updated)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(vin) DO NOTHING
        """,
        (vin, stock_number, CACHE_VERSION, now, now),
    )


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


def get_vehicle(vin: str) -> dict[str, Any] | None:
    """Return all cached data for a VIN as a dict, or None if not cached."""
    row = _get_row(vin)
    return dict(row) if row else None


def get_vehicle_by_stock(stock_number: str) -> dict[str, Any] | None:
    """Reverse lookup: the cached vehicle_data row for this stock number, or
    None. stock_number isn't the primary key (vin is) and isn't guaranteed
    unique across the table's history — a VIN can pick up a new stock number
    after a lot move — so this returns the most recently updated match."""
    if not stock_number:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM vehicle_data WHERE stock_number = ? "
            "ORDER BY last_updated DESC LIMIT 1",
            (stock_number,),
        ).fetchone()
    return dict(row) if row else None


def get_window_sticker(vin: str) -> dict[str, Any] | None:
    """Return cached window sticker data, or None if missing. Never expires."""
    row = _get_row(vin)
    if not row or not row["window_sticker_json"]:
        return None
    if not _is_fresh(row["window_sticker_date"], WINDOW_STICKER_TTL):
        return None
    return json.loads(row["window_sticker_json"])


def get_carfax(vin: str) -> dict[str, Any] | None:
    """Return cached Carfax data if less than CARFAX_TTL days old, else None."""
    row = _get_row(vin)
    if not row or not row["carfax_json"]:
        return None
    if not _is_fresh(row["carfax_date"], CARFAX_TTL):
        return None
    return json.loads(row["carfax_json"])


def get_carfax_image_path(vin: str) -> str | None:
    """Path to the cached Carfax report screenshot for this VIN (see
    vision_parser.parse_carfax_image()), or None if never captured. Not
    freshness-gated by CARFAX_TTL like get_carfax() — a stale row's image is
    still the same file on disk and still worth reusing."""
    row = _get_row(vin)
    return row["carfax_image_path"] if row else None


def get_sticker_image_path(vin: str) -> str | None:
    """Path to the cached window-sticker image for this VIN (see
    vision_parser.parse_sticker_image()), or None if never captured."""
    row = _get_row(vin)
    return row["sticker_image_path"] if row else None


def get_recon_image_path(vin: str) -> str | None:
    """Path to the cached ReconVision work-order screenshot for this VIN (see
    vision_parser.parse_recon_image()), or None if never captured. Not
    freshness-gated like get_recon() — a cached row's image is still the
    same file on disk and still worth reusing."""
    row = _get_row(vin)
    return row["recon_image_path"] if row else None


def get_recon(vin: str) -> dict[str, Any] | None:
    """Return cached recon data if recon_complete is True (any age). Returns
    None if missing or recon_complete is False, forcing a re-scrape."""
    row = _get_row(vin)
    if not row or not row["recon_json"]:
        return None
    if not row["recon_complete"]:
        return None
    return json.loads(row["recon_json"])


# --------------------------------------------------------------------------- #
# "do we need to scrape this?" — the inverse of a cache hit
# --------------------------------------------------------------------------- #


def needs_window_sticker(vin: str) -> bool:
    """True if there's no usable cached window sticker for this VIN (never
    expires, so this is just "have we ever cached one")."""
    return get_window_sticker(vin) is None


def needs_carfax(vin: str) -> bool:
    """True if there's no Carfax cached for this VIN, or it's older than
    CARFAX_TTL days."""
    return get_carfax(vin) is None


def needs_recon(vin: str) -> bool:
    """True if there's no recon cached for this VIN, or the cached recon is
    not yet complete (forces a re-scrape until Close RO shows TASK COMPLETED)."""
    return get_recon(vin) is None


def get_autoipacket_attempts(vin: str) -> int:
    """Current AutoiPacket-failure count for this VIN (non-MB retry-exhaustion
    fallback). 0 if never attempted or the VIN isn't cached yet."""
    row = _get_row(vin)
    if not row or row["autoipacket_attempts"] is None:
        return 0
    return int(row["autoipacket_attempts"])


# --------------------------------------------------------------------------- #
# writes
# --------------------------------------------------------------------------- #


def save_window_sticker(
    vin: str,
    stock_number: str | None,
    year_make_model: str | None,
    sticker_data: dict[str, Any],
    source: str,
    image_path: str | None = None,
) -> None:
    """Upsert window sticker data. Sets window_sticker_date to today.
    image_path (see vision_parser.py) is optional and left unchanged (via
    COALESCE) when not given, so a call that doesn't know about it never
    wipes out a path an earlier save already recorded."""
    now = datetime.now().isoformat()
    today = date.today().isoformat()
    with _connect() as conn:
        _ensure_row(conn, vin, stock_number)
        conn.execute(
            """
            UPDATE vehicle_data SET
                stock_number          = ?,
                year_make_model       = ?,
                window_sticker_json   = ?,
                window_sticker_date   = ?,
                window_sticker_source = ?,
                sticker_image_path    = COALESCE(?, sticker_image_path),
                last_updated          = ?
            WHERE vin = ?
            """,
            (
                stock_number,
                year_make_model,
                json.dumps(sticker_data),
                today,
                source,
                image_path,
                now,
                vin,
            ),
        )
        conn.commit()


def save_carfax(
    vin: str,
    stock_number: str | None,
    carfax_data: dict[str, Any],
    image_path: str | None = None,
) -> None:
    """Upsert Carfax data. Sets carfax_date to today. image_path (see
    vision_parser.py) is optional and left unchanged (via COALESCE) when not
    given, so a call that doesn't know about it never wipes out a path an
    earlier save already recorded."""
    now = datetime.now().isoformat()
    today = date.today().isoformat()
    with _connect() as conn:
        _ensure_row(conn, vin, stock_number)
        conn.execute(
            """
            UPDATE vehicle_data SET
                stock_number      = ?,
                carfax_json       = ?,
                carfax_date       = ?,
                carfax_image_path = COALESCE(?, carfax_image_path),
                last_updated      = ?
            WHERE vin = ?
            """,
            (stock_number, json.dumps(carfax_data), today, image_path, now, vin),
        )
        conn.commit()


def get_seller_comments(vin: str) -> str | None:
    """Free-text seller comments for this VIN (entered on the Database page),
    or None when none are saved. Used verbatim at the end of paragraph one."""
    row = _get_row(vin)
    text = (row["seller_comments"] if row else None) or ""
    return text.strip() or None


def save_seller_comments(vin: str, text: str | None, stock_number: str | None = None) -> None:
    """Upsert the seller comments for a VIN (creating the row if needed).
    Blank text clears them."""
    now = datetime.now().isoformat()
    value = (text or "").strip() or None
    with _connect() as conn:
        _ensure_row(conn, vin, stock_number)
        conn.execute(
            "UPDATE vehicle_data SET seller_comments = ?, last_updated = ? WHERE vin = ?",
            (value, now, vin),
        )
        conn.commit()


def save_recon(
    vin: str,
    stock_number: str | None,
    recon_data: dict[str, Any],
    recon_complete: bool,
    image_path: str | None = None,
) -> None:
    """Upsert recon data. Sets recon_date to today. image_path (see
    vision_parser.py) is optional and left unchanged (via COALESCE) when not
    given, so a call that doesn't know about it never wipes out a path an
    earlier save already recorded.

    recon_complete never downgrades from True to False: once a vehicle's
    recon has been fully scraped, a later partial/incomplete scrape can still
    update recon_json but must not flip recon_complete back to False.
    """
    now = datetime.now().isoformat()
    today = date.today().isoformat()
    with _connect() as conn:
        _ensure_row(conn, vin, stock_number)
        existing = conn.execute(
            "SELECT recon_complete FROM vehicle_data WHERE vin = ?", (vin,)
        ).fetchone()
        already_complete = bool(existing["recon_complete"]) if existing else False
        final_complete = 1 if (recon_complete or already_complete) else 0

        conn.execute(
            """
            UPDATE vehicle_data SET
                stock_number      = ?,
                recon_json        = ?,
                recon_date        = ?,
                recon_complete    = ?,
                recon_image_path  = COALESCE(?, recon_image_path),
                last_updated      = ?
            WHERE vin = ?
            """,
            (stock_number, json.dumps(recon_data), today, final_complete, image_path, now, vin),
        )
        conn.commit()


def increment_autoipacket_attempts(vin: str) -> None:
    """Bump the AutoiPacket-failure counter for this VIN — the non-MB
    retry-exhaustion fallback stops retrying and proceeds without MSRP once
    get_autoipacket_attempts(vin) reaches 3. Creates the row if it doesn't
    exist yet."""
    now = datetime.now().isoformat()
    with _connect() as conn:
        _ensure_row(conn, vin, None)
        conn.execute(
            """
            UPDATE vehicle_data SET
                autoipacket_attempts = autoipacket_attempts + 1,
                last_updated         = ?
            WHERE vin = ?
            """,
            (now, vin),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #


def get_cache_stats() -> dict[str, int]:
    """Aggregate counts across the whole cache, for a quick health check
    without hand-rolling a query each time."""
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM vehicle_data").fetchone()[0]
        window_stickers = conn.execute(
            "SELECT COUNT(*) FROM vehicle_data WHERE window_sticker_json IS NOT NULL"
        ).fetchone()[0]
        carfax = conn.execute(
            "SELECT COUNT(*) FROM vehicle_data WHERE carfax_json IS NOT NULL"
        ).fetchone()[0]
        recon_cached = conn.execute(
            "SELECT COUNT(*) FROM vehicle_data WHERE recon_json IS NOT NULL"
        ).fetchone()[0]
        recon_complete = conn.execute(
            "SELECT COUNT(*) FROM vehicle_data WHERE recon_complete = 1"
        ).fetchone()[0]
        recon_pending = conn.execute(
            "SELECT COUNT(*) FROM vehicle_data "
            "WHERE recon_json IS NOT NULL AND recon_complete = 0"
        ).fetchone()[0]

    return {
        "total_vehicles": total,
        "window_stickers_cached": window_stickers,
        "carfax_cached": carfax,
        "recon_cached": recon_cached,
        "recon_complete": recon_complete,
        "recon_pending": recon_pending,
    }


# --------------------------------------------------------------------------- #
# CLI — quick inspection
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    with _connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM vehicle_data").fetchone()[0]
        print(f"vehicle_cache.db  ({DB_PATH})")
        print(f"  vehicle_data: {n} row(s)")
        for r in conn.execute(
            "SELECT vin, stock_number, year_make_model, "
            "window_sticker_date, carfax_date, recon_date, recon_complete "
            "FROM vehicle_data ORDER BY last_updated DESC LIMIT 20"
        ):
            print(
                f"    {r['vin']}  {r['stock_number'] or ''}  "
                f"{r['year_make_model'] or ''}  "
                f"sticker={r['window_sticker_date'] or '-'}  "
                f"carfax={r['carfax_date'] or '-'}  "
                f"recon={r['recon_date'] or '-'} "
                f"(complete={bool(r['recon_complete'])})"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
