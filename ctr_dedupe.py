#!/usr/bin/env python3
"""ctr_dedupe.py — remove duplicate ctr_history rows and make them impossible.

    python ctr_dedupe.py            # dry run: counts and the rows that would go
    python ctr_dedupe.py --apply    # back up the database, save the extra rows to a file,
                                    # delete them, add the unique index

A duplicate is more than one row for the same (date, dealership_name,
stock_number). The LAST row (highest id) of each group is kept. Before anything
is deleted: ctr_history.db is copied to ctr_history.db.backup-<date>-dedupe and
the rows to be removed are written, complete, to ctr_history_duplicates_<date>.json.

Afterwards the unique index ux_ctr_history_day_store_stock exists, and
ctr_database.record_ctr upserts on it, so a restarted CTR step that reads a
vehicle again replaces that day's row instead of adding a second one.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path

import ctr_database as C

UNIQUE_INDEX = "ux_ctr_history_day_store_stock"


def extra_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every row that is not the last one of its (date, store, stock) group."""
    return conn.execute(
        """SELECT * FROM ctr_history t
            WHERE stock_number IS NOT NULL
              AND id < (SELECT MAX(id) FROM ctr_history
                         WHERE date = t.date AND dealership_name IS t.dealership_name AND stock_number = t.stock_number)
            ORDER BY date, dealership_name, stock_number, id"""
    ).fetchall()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=str(C.DB_PATH))
    args = ap.parse_args(argv)
    db = Path(args.db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) FROM ctr_history").fetchone()[0]
    extras = extra_rows(conn)
    groups = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM ctr_history WHERE stock_number IS NOT NULL "
        "GROUP BY date, dealership_name, stock_number HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    by_day: dict[tuple, int] = {}
    for r in extras:
        by_day[(r["date"], r["dealership_name"])] = by_day.get((r["date"], r["dealership_name"]), 0) + 1
    print(f"{'APPLY' if args.apply else 'DRY RUN'}: {total:,} rows; {groups} duplicate groups; {len(extras)} extra rows")
    for (d, store), n in sorted(by_day.items()):
        print(f"  {d}  {store:32s} {n}")
    if not args.apply:
        print("nothing written; add --apply")
        return 0
    stamp = date.today().isoformat()
    backup = db.with_name(f"{db.name}.backup-{stamp}-dedupe")
    if backup.exists():
        sys.exit(f"{backup.name} already exists; not overwriting")
    conn.close()
    shutil.copy2(db, backup)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    extras = extra_rows(conn)
    out = db.with_name(f"ctr_history_duplicates_{stamp}.json")
    out.write_text(json.dumps([dict(r) for r in extras], indent=1), encoding="utf-8")
    print(f"backed up -> {backup.name}; {len(extras)} extra rows written -> {out.name}")
    ids = [r["id"] for r in extras]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        conn.execute(f"DELETE FROM ctr_history WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {UNIQUE_INDEX} ON ctr_history (date, dealership_name, stock_number)")
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM ctr_history").fetchone()[0]
    left = len(extra_rows(conn))
    print(f"deleted {len(ids)}; rows {total:,} -> {after:,}; duplicates left {left}; unique index {UNIQUE_INDEX} in place")
    conn.close()
    return 0 if left == 0 and after == total - len(ids) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
