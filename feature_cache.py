#!/usr/bin/env python3
"""feature_cache.py — local cache for brand feature blurbs, tow ratings, and
per-trim standard equipment + engine specs.

Three SQLite tables in feature_cache.db:

  brand_features   — branded feature descriptions at the manufacturer level
                     (not model/year specific). e.g. "Magic Sky Control".
  towing_capacity  — tow ratings at the model + year + trim level, because the
                     numbers change year to year.
  trim_knowledge   — standard equipment and verified engine description at the
                     exact year + make + model + trim level (non-Mercedes only).

    from feature_cache import (
        get_feature, save_feature, get_towing, save_towing,
        get_trim_knowledge, save_trim_knowledge,
    )
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).with_name("feature_cache.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS brand_features (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    brand         TEXT NOT NULL,
    feature_name  TEXT NOT NULL COLLATE NOCASE,
    description   TEXT,
    source_url    TEXT,
    cached_date   TEXT,
    UNIQUE (brand, feature_name)
);

CREATE TABLE IF NOT EXISTS towing_capacity (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    year              INTEGER NOT NULL,
    make              TEXT NOT NULL COLLATE NOCASE,
    model             TEXT NOT NULL COLLATE NOCASE,
    trim              TEXT COLLATE NOCASE,
    tow_rating_lbs    INTEGER,
    package_required  INTEGER NOT NULL DEFAULT 0,
    package_name      TEXT,
    source_url        TEXT,
    cached_date       TEXT,
    UNIQUE (year, make, model, trim, package_required)
);

CREATE TABLE IF NOT EXISTS trim_knowledge (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    year                INTEGER NOT NULL,
    make                TEXT NOT NULL COLLATE NOCASE,
    model               TEXT NOT NULL COLLATE NOCASE,
    trim                TEXT NOT NULL COLLATE NOCASE,
    standard_equipment  TEXT,
    engine_description  TEXT,
    source_url          TEXT,
    cached_date         TEXT,
    UNIQUE (year, make, model, trim)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# brand_features
# --------------------------------------------------------------------------- #


def get_feature(brand: str, feature_name: str) -> dict[str, Any] | None:
    """Look up a branded feature. Case-insensitive on feature_name. Returns the
    full row as a dict, or None if it isn't cached yet."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM brand_features "
            "WHERE brand = ? AND feature_name = ? COLLATE NOCASE "
            "LIMIT 1",
            (brand, feature_name),
        ).fetchone()
    return dict(row) if row else None


def save_feature(
    brand: str,
    feature_name: str,
    description: str | None,
    source_url: str | None,
) -> None:
    """Insert or update a feature record (keyed on brand + feature_name,
    case-insensitive on feature_name). cached_date is set to today."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO brand_features (brand, feature_name, description, source_url, cached_date)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(brand, feature_name) DO UPDATE SET
                description = excluded.description,
                source_url  = excluded.source_url,
                cached_date = excluded.cached_date
            """,
            (brand, feature_name, description, source_url, date.today().isoformat()),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# towing_capacity
# --------------------------------------------------------------------------- #


def get_towing(
    year: int,
    make: str,
    model: str,
    trim: str | None = None,
    package_required: bool | int | None = None,
) -> dict[str, Any] | None:
    """Look up a tow rating. Tries an exact match (year/make/model/trim, and
    package_required when supplied) first; if nothing matches, falls back to a
    model-level match that ignores trim."""
    pkg = None if package_required is None else (1 if package_required else 0)

    with _connect() as conn:
        # Tier 1 — exact match on everything provided.
        where = ["year = ?", "make = ?", "model = ?", "trim IS ?"]
        params: list[Any] = [year, make, model, trim]
        if pkg is not None:
            where.append("package_required = ?")
            params.append(pkg)
        row = conn.execute(
            f"SELECT * FROM towing_capacity WHERE {' AND '.join(where)} "
            f"ORDER BY package_required ASC LIMIT 1",
            params,
        ).fetchone()
        if row:
            return dict(row)

        # Tier 2 — model level, ignoring trim.
        where = ["year = ?", "make = ?", "model = ?"]
        params = [year, make, model]
        if pkg is not None:
            where.append("package_required = ?")
            params.append(pkg)
        row = conn.execute(
            f"SELECT * FROM towing_capacity WHERE {' AND '.join(where)} "
            f"ORDER BY (trim IS NULL) DESC, package_required ASC, "
            f"tow_rating_lbs DESC LIMIT 1",
            params,
        ).fetchone()
    return dict(row) if row else None


def save_towing(
    year: int,
    make: str,
    model: str,
    tow_rating_lbs: int | None,
    trim: str | None = None,
    package_required: bool | int = False,
    package_name: str | None = None,
    source_url: str | None = None,
) -> None:
    """Insert or update a tow rating, keyed on
    (year, make, model, trim, package_required). cached_date is set to today."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO towing_capacity
                (year, make, model, trim, tow_rating_lbs, package_required,
                 package_name, source_url, cached_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(year, make, model, trim, package_required) DO UPDATE SET
                tow_rating_lbs = excluded.tow_rating_lbs,
                package_name   = excluded.package_name,
                source_url     = excluded.source_url,
                cached_date    = excluded.cached_date
            """,
            (
                year,
                make,
                model,
                trim,
                tow_rating_lbs,
                1 if package_required else 0,
                package_name,
                source_url,
                date.today().isoformat(),
            ),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# trim_knowledge
# --------------------------------------------------------------------------- #


def get_trim_knowledge(
    year: int, make: str, model: str, trim: str | None
) -> dict[str, Any] | None:
    """Look up cached standard-equipment + engine info for this exact
    year/make/model/trim. Case-insensitive. Returns None if not cached yet."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM trim_knowledge "
            "WHERE year = ? AND make = ? COLLATE NOCASE AND model = ? COLLATE NOCASE "
            "AND trim = ? COLLATE NOCASE LIMIT 1",
            (year, make, model, trim or ""),
        ).fetchone()
    return dict(row) if row else None


def save_trim_knowledge(
    year: int,
    make: str,
    model: str,
    trim: str | None,
    standard_equipment: str | None,
    engine_description: str | None,
    source_url: str | None,
) -> None:
    """Insert or update trim knowledge (upsert on year/make/model/trim).

    A field passed as None never overwrites a value already stored: a partial
    save (e.g. only an engine description salvaged from prose) must not wipe
    out a standard-equipment list an earlier, fuller save recorded."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO trim_knowledge
                (year, make, model, trim, standard_equipment, engine_description,
                 source_url, cached_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(year, make, model, trim) DO UPDATE SET
                standard_equipment = COALESCE(excluded.standard_equipment, standard_equipment),
                engine_description = COALESCE(excluded.engine_description, engine_description),
                source_url         = COALESCE(excluded.source_url, source_url),
                cached_date        = excluded.cached_date
            """,
            (
                year,
                make,
                model,
                trim or "",
                standard_equipment,
                engine_description,
                source_url,
                date.today().isoformat(),
            ),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# CLI — quick inspection
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    with _connect() as conn:
        nf = conn.execute("SELECT COUNT(*) FROM brand_features").fetchone()[0]
        nt = conn.execute("SELECT COUNT(*) FROM towing_capacity").fetchone()[0]
        nk = conn.execute("SELECT COUNT(*) FROM trim_knowledge").fetchone()[0]
        print(f"feature_cache.db  ({DB_PATH})")
        print(f"  brand_features:  {nf} row(s)")
        print(f"  towing_capacity: {nt} row(s)")
        print(f"  trim_knowledge:  {nk} row(s)")
        for r in conn.execute(
            "SELECT brand, feature_name, cached_date FROM brand_features "
            "ORDER BY brand, feature_name LIMIT 20"
        ):
            print(f"    [{r['brand']}] {r['feature_name']}  ({r['cached_date']})")
        for r in conn.execute(
            "SELECT year, make, model, trim, tow_rating_lbs, package_required "
            "FROM towing_capacity ORDER BY make, model, year LIMIT 20"
        ):
            pkg = " +pkg" if r["package_required"] else ""
            print(
                f"    {r['year']} {r['make']} {r['model']} "
                f"{r['trim'] or ''}  {r['tow_rating_lbs']} lbs{pkg}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
