#!/usr/bin/env python3
"""ctr_database.py — SQLite store for daily click-through-rate (CTR) snapshots.

One row per vehicle per day: the latest AutoTrader CTR and the market-average
CTR from ACV MAX's "Price In the Game" graph, plus the vehicle facts at that
moment and whether an ad had been written yet.

    from ctr_database import record_ctr, get_ctr_history, get_latest_ctr

CLI:
    python ctr_database.py <STOCK#>     # print a vehicle's CTR history
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).with_name("ctr_history.db")

BENCHMARK_DEALERSHIPS = (
    "Mercedes-Benz of Northlake",
    "Hendrick Motors of Charlotte",
)
_SUMMARY_TIERS = ("mb_cpo", "hendrick_certified", "hendrick_affordable")

DEFAULT_DEALERSHIP = "Mercedes-Benz of Durham"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ctr_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    date              TEXT NOT NULL,
    stock_number      TEXT,
    vin               TEXT,
    year_make_model   TEXT,
    current_price     REAL,
    days_on_lot       INTEGER,
    autotrader_ctr    REAL,
    cargurus_ctr      REAL,
    average_ctr       REAL,
    ad_written        INTEGER NOT NULL DEFAULT 0,
    ad_written_date   TEXT,
    scraped_at        TEXT,
    dealership_name   TEXT DEFAULT 'Mercedes-Benz of Durham',
    dealership_role   TEXT DEFAULT 'primary',
    certification_tier TEXT,
    status_code       INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ctr_history_stock_date
    ON ctr_history (stock_number, date);
CREATE INDEX IF NOT EXISTS ix_ctr_history_dealer_date
    ON ctr_history (dealership_name, date);
"""

# Columns added after the first release — applied to pre-existing databases.
_MIGRATIONS = {
    "cargurus_ctr": "ALTER TABLE ctr_history ADD COLUMN cargurus_ctr REAL",
    "dealership_name": (
        "ALTER TABLE ctr_history ADD COLUMN dealership_name TEXT "
        "DEFAULT 'Mercedes-Benz of Durham'"
    ),
    "dealership_role": (
        "ALTER TABLE ctr_history ADD COLUMN dealership_role TEXT DEFAULT 'primary'"
    ),
    "certification_tier": "ALTER TABLE ctr_history ADD COLUMN certification_tier TEXT",
    "status_code": "ALTER TABLE ctr_history ADD COLUMN status_code INTEGER",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(ctr_history)")}
    for col, ddl in _MIGRATIONS.items():
        if col not in have:
            conn.execute(ddl)
    conn.commit()
    return conn


def infer_tier(
    status_code: int | str | None = None,
    certified: bool | None = None,
    price: float | int | None = None,
) -> str:
    """Best-effort certification tier for a vehicle.

    With a status code (the primary store): 10/16 -> mb_cpo, 11 ->
    hendrick_certified, 12 -> hendrick_affordable, anything else -> unknown.

    Without a status code (benchmark stores): fall back to the certified flag and
    price -> certified & price >= $30k -> mb_cpo; certified & price < $30k ->
    hendrick_certified; not certified -> hendrick_affordable; otherwise unknown.
    """
    if status_code is not None and str(status_code).strip() != "":
        try:
            sc = int(status_code)
        except (TypeError, ValueError):
            return "unknown"
        if sc in (10, 16):
            return "mb_cpo"
        if sc == 11:
            return "hendrick_certified"
        if sc == 12:
            return "hendrick_affordable"
        return "unknown"

    if certified is True:
        try:
            p = float(price) if price is not None else None
        except (TypeError, ValueError):
            p = None
        if p is None:
            return "unknown"
        return "mb_cpo" if p >= 30_000 else "hendrick_certified"
    if certified is False:
        return "hendrick_affordable"
    return "unknown"


def record_ctr(
    vehicle_data: dict[str, Any],
    ctr_data: dict[str, Any],
    ad_written: bool = False,
    ad_written_date: str | None = None,
    *,
    dealership_name: str = "Mercedes-Benz of Durham",
    dealership_role: str = "primary",
    certification_tier: str | None = None,
    status_code: int | None = None,
) -> int:
    """Insert one CTR snapshot row. Returns the new row id.

    vehicle_data: the aggregator's `vehicle` dict (stock_number, vin,
        year_make_model, current_price, days_on_lot, and — for the primary store —
        status_code / certified).
    ctr_data: the scrape_ctr() return (latest_autotrader_ctr,
        latest_cargurus_ctr, latest_average_ctr, scraped_at).

    dealership_name / dealership_role / certification_tier / status_code are all
    optional. When status_code is not passed it falls back to
    vehicle_data["status_code"]; when certification_tier is not passed it is
    inferred with infer_tier(). Existing 4-arg calls keep working unchanged.
    """
    v = vehicle_data or {}
    c = ctr_data or {}

    if status_code is None:
        status_code = v.get("status_code")
    if certification_tier is None:
        certification_tier = infer_tier(
            status_code=status_code,
            certified=v.get("certified"),
            price=v.get("current_price"),
        )

    row = (
        date.today().isoformat(),
        v.get("stock_number"),
        v.get("vin"),
        v.get("year_make_model"),
        v.get("current_price"),
        v.get("days_on_lot"),
        c.get("latest_autotrader_ctr"),
        c.get("latest_cargurus_ctr"),
        c.get("latest_average_ctr"),
        1 if ad_written else 0,
        ad_written_date,
        c.get("scraped_at") or datetime.now(timezone.utc).isoformat(),
        dealership_name,
        dealership_role,
        certification_tier,
        status_code,
    )
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO ctr_history ("
            "date, stock_number, vin, year_make_model, current_price, "
            "days_on_lot, autotrader_ctr, cargurus_ctr, average_ctr, ad_written, "
            "ad_written_date, scraped_at, dealership_name, dealership_role, "
            "certification_tier, status_code"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        conn.commit()
        return int(cur.lastrowid)


def get_ctr_history(
    stock_number: str, dealership_name: str | None = None
) -> list[dict[str, Any]]:
    """All snapshot rows for a vehicle, oldest first. Optionally scope to one
    dealership."""
    q = "SELECT * FROM ctr_history WHERE stock_number = ?"
    params: list[Any] = [stock_number]
    if dealership_name:
        q += " AND dealership_name = ?"
        params.append(dealership_name)
    q += " ORDER BY date ASC, id ASC"
    with _connect() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_latest_ctr(
    stock_number: str, dealership_name: str | None = None
) -> dict[str, Any] | None:
    """The most recent snapshot row for a vehicle, or None. Optionally scope to
    one dealership."""
    q = "SELECT * FROM ctr_history WHERE stock_number = ?"
    params: list[Any] = [stock_number]
    if dealership_name:
        q += " AND dealership_name = ?"
        params.append(dealership_name)
    q += " ORDER BY date DESC, id DESC LIMIT 1"
    with _connect() as conn:
        row = conn.execute(q, params).fetchone()
    return dict(row) if row else None


def get_ctr_by_tier(
    tier: str, dealership_name: str | None = None
) -> list[dict[str, Any]]:
    """Every CTR record for a certification tier, oldest first. Optionally scope
    to one dealership. This is the core analysis query."""
    q = "SELECT * FROM ctr_history WHERE certification_tier = ?"
    params: list[Any] = [tier]
    if dealership_name:
        q += " AND dealership_name = ?"
        params.append(dealership_name)
    q += " ORDER BY date ASC, id ASC"
    with _connect() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_ctr_summary(days: int = 30) -> dict[str, dict[str, dict[str, Any]]]:
    """Rolling {dealership -> {tier -> {avg_autotrader_ctr, avg_cargurus_ctr,
    vehicle_count}}} over the last `days` days. This is what the dashboard calls.

    Every known dealership (primary + benchmarks) and the three real tiers are
    always present; empty cells read {..: None, "vehicle_count": 0}.
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT dealership_name AS dn, "
            "COALESCE(certification_tier, 'unknown') AS tier, "
            "AVG(autotrader_ctr) AS at_avg, AVG(cargurus_ctr) AS cg_avg, "
            "COUNT(DISTINCT stock_number) AS n "
            "FROM ctr_history WHERE date >= ? "
            "GROUP BY dealership_name, tier",
            (cutoff,),
        ).fetchall()

    dealers: list[str] = ["Mercedes-Benz of Durham", *BENCHMARK_DEALERSHIPS]
    for r in rows:
        if r["dn"] and r["dn"] not in dealers:
            dealers.append(r["dn"])

    out: dict[str, dict[str, dict[str, Any]]] = {
        d: {
            t: {"avg_autotrader_ctr": None, "avg_cargurus_ctr": None, "vehicle_count": 0}
            for t in _SUMMARY_TIERS
        }
        for d in dealers
    }
    for r in rows:
        dn = r["dn"] or "Mercedes-Benz of Durham"
        out.setdefault(dn, {})
        out[dn][r["tier"]] = {
            "avg_autotrader_ctr": round(r["at_avg"], 4) if r["at_avg"] is not None else None,
            "avg_cargurus_ctr": round(r["cg_avg"], 4) if r["cg_avg"] is not None else None,
            "vehicle_count": r["n"],
        }
    return out


# --------------------------------------------------------------------------- #
# Daily CTR email
# --------------------------------------------------------------------------- #
#
# NOTE: this builds and sends the email from whatever is already in
# ctr_history.db for today — it does not itself scrape Northlake/Charlotte or
# Durham. Something upstream (orchestrator.py's CTR capture step, or a
# benchmark scraper) has to have called record_ctr() for today's rows to exist
# first. --daily-scrape below is meant to run after that capture step, not
# instead of it.

_DUE_TIER_LABELS = (
    ("hendrick_certified", "Hendrick Certified"),
    ("hendrick_affordable", "Hendrick Affordable"),
)


def _pct(x: float | None) -> str:
    return f"{x:.2f}%" if isinstance(x, (int, float)) else "n/a"


def _dedup_by_stock(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse multiple same-day rows for the same stock number down to one —
    the row with the highest cargurus_ctr. A vehicle whose CTR gets captured
    more than once in a day (e.g. by two separate scrape steps) must still
    appear only once in any report built from this."""
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        stock = r.get("stock_number")
        if not stock:
            continue
        cg = r.get("cargurus_ctr")
        cur = best.get(stock)
        if cur is None:
            best[stock] = r
            continue
        cur_cg = cur.get("cargurus_ctr")
        if cg is not None and (cur_cg is None or cg > cur_cg):
            best[stock] = r
    return list(best.values())


def _avg_cargurus_ctr(rows: list[dict[str, Any]]) -> float | None:
    vals = [r["cargurus_ctr"] for r in rows if r.get("cargurus_ctr") is not None]
    return (sum(vals) / len(vals)) if vals else None


def _tier_lines(rows: list[dict[str, Any]], label: str, *, show_avg: bool = True) -> list[str]:
    out = [f"{label}: {len(rows)} vehicles tracked"]
    if show_avg:
        out.append(f"  Average CarGurus CTR: {_pct(_avg_cargurus_ctr(rows))}")
    return out


def _vehicle_line(r: dict[str, Any]) -> str:
    return (
        f"  [{r.get('stock_number') or '?'}]  "
        f"{r.get('year_make_model') or 'unknown'}  —  {_pct(r.get('cargurus_ctr'))}"
    )


def format_daily_ctr_email(*, today: date | None = None) -> tuple[str, str]:
    """Build (subject, body) for the CTR daily email from today's
    ctr_history.db rows. Deduplicated per _dedup_by_stock() throughout, so a
    vehicle scraped more than once today never appears twice."""
    today = today or date.today()
    today_iso = today.isoformat()

    with _connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM ctr_history WHERE date = ?", (today_iso,)
            )
        ]

    by_dealer: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_dealer.setdefault(r.get("dealership_name") or DEFAULT_DEALERSHIP, []).append(r)

    durham = _dedup_by_stock(by_dealer.get(DEFAULT_DEALERSHIP, []))
    northlake = _dedup_by_stock(by_dealer.get("Mercedes-Benz of Northlake", []))
    charlotte = _dedup_by_stock(by_dealer.get("Hendrick Motors of Charlotte", []))
    all_deduped = durham + northlake + charlotte

    durham_cpo = [r for r in durham if r.get("certification_tier") == "mb_cpo"]
    durham_hc = [r for r in durham if r.get("certification_tier") == "hendrick_certified"]
    durham_ha = [r for r in durham if r.get("certification_tier") == "hendrick_affordable"]
    durham_unknown = [
        r for r in durham
        if r.get("certification_tier") not in ("mb_cpo", "hendrick_certified", "hendrick_affordable")
    ]

    durham_avg = _avg_cargurus_ctr(durham_cpo)
    northlake_avg = _avg_cargurus_ctr(northlake)
    charlotte_avg = _avg_cargurus_ctr(charlotte)

    # --- subject: date + the three store averages at a glance ------------- #
    subject = (
        f"CTR Update {today.month}/{today.day} — "
        f"Durham {_pct(durham_avg)} | Northlake {_pct(northlake_avg)} | "
        f"Charlotte {_pct(charlotte_avg)}"
    )

    # --- body --------------------------------------------------------- #
    dates_seen = [r["date"] for r in all_deduped if r.get("date")]
    period_start = min(dates_seen) if dates_seen else today_iso
    period_end = max(dates_seen) if dates_seen else today_iso

    rule = "=" * 48
    lines = [
        rule,
        f"CTR DAILY SCRAPE — {today_iso}",
        f"Data period: {period_start} to {period_end}",
        f"Run completed: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Vehicles tracked: {len(all_deduped)}",
        rule,
        "",
    ]

    ranked = sorted(
        (r for r in durham_cpo if r.get("cargurus_ctr") is not None),
        key=lambda r: r["cargurus_ctr"],
        reverse=True,
    )
    lines.append("TOP 5 — Durham CPO (CarGurus CTR)")
    lines.append(rule)
    lines += [_vehicle_line(r) for r in ranked[:5]] or ["(no data)"]

    lines += ["", "BOTTOM 5 — Durham CPO (CarGurus CTR)", rule]
    bottom5 = list(reversed(ranked[-5:])) if ranked else []
    lines += [_vehicle_line(r) for r in bottom5] or ["(no data)"]

    lines += ["", "OTHER TRACKED VEHICLES", rule]
    lines += _tier_lines(durham_hc, "Hendrick Certified")
    lines += _tier_lines(durham_ha, "Hendrick Affordable")
    lines += _tier_lines(durham_unknown, "Unknown tier", show_avg=False)

    return subject, "\n".join(lines) + "\n"


def send_daily_ctr_email(*, send: bool = True, today: date | None = None) -> tuple[str, str]:
    """Build the daily CTR email and send it. Returns (subject, body) either
    way, so callers/tests can inspect it without actually sending."""
    from adwriter import _send_gmail

    subject, body = format_daily_ctr_email(today=today)
    if send:
        _send_gmail(subject, body)
    return subject, body


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(
            "usage: python ctr_database.py <STOCK#> | --summary [DAYS] | "
            "--daily-scrape",
            file=sys.stderr,
        )
        return 1

    if args[0] in ("--summary", "-s"):
        days = int(args[1]) if len(args) > 1 else 30
        import json

        print(f"CTR summary — last {days} days  (db: {DB_PATH})")
        print(json.dumps(get_ctr_summary(days), indent=2))
        return 0

    if args[0] == "--daily-scrape":
        subject, _body = send_daily_ctr_email()
        print(f"[ctr] daily email sent: {subject}")
        return 0

    stock = args[0].strip().lstrip("#").upper()
    rows = get_ctr_history(stock)
    if not rows:
        print(f"No CTR history for {stock} (db: {DB_PATH}).")
        return 0
    print(f"CTR history for {stock}  ({len(rows)} row(s), db: {DB_PATH})")
    print(
        f"{'date':<12} {'dealership':<26} {'tier':<18} {'price':>10} {'DOL':>4} "
        f"{'AT CTR':>8} {'CG CTR':>8} {'avg CTR':>8} {'ad?':>4}"
    )

    def _c(x) -> str:
        return "" if x is None else str(x)

    for r in rows:
        print(
            f"{r['date']:<12} {(_c(r['dealership_name']) or 'n/a'):<26} "
            f"{(_c(r['certification_tier']) or 'n/a'):<18} "
            f"{_c(r['current_price']):>10} {_c(r['days_on_lot']):>4} "
            f"{_c(r['autotrader_ctr']):>8} {_c(r['cargurus_ctr']):>8} "
            f"{_c(r['average_ctr']):>8} {'yes' if r['ad_written'] else 'no':>4}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
